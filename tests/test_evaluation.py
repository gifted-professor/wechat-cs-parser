from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from wechat_cs.evaluation import (
    ScoredExample,
    StrategyObservation,
    audit_blind_payloads,
    evaluate_baselines,
    evaluate_rollout,
    select_strategy_statistics,
)


UTC8 = timezone(timedelta(hours=8))


class OfflineEvaluationTests(unittest.TestCase):
    def test_time_out_customer_isolation_and_three_baselines(self) -> None:
        examples = [
            ScoredExample(
                customer_key="train-shared",
                as_of_at=datetime(2026, 6, 1, tzinfo=UTC8),
                paid_7d=True,
                rfm_score=0.9,
                chat_score=0.9,
                combined_score=0.9,
            ),
            # This tempting future row must be excluded because the same customer
            # was already present before the time cut.
            ScoredExample(
                customer_key="train-shared",
                as_of_at=datetime(2026, 7, 2, tzinfo=UTC8),
                paid_7d=True,
                rfm_score=1.0,
                chat_score=1.0,
                combined_score=1.0,
            ),
        ]
        for index in range(120):
            positive = index < 24
            examples.append(
                ScoredExample(
                    customer_key="test-%03d" % index,
                    as_of_at=datetime(2026, 7, 2, tzinfo=UTC8),
                    paid_7d=positive,
                    rfm_score=(120 - index) / 120,
                    chat_score=(index + 1) / 120,
                    combined_score=(120 - index) / 120,
                    retained_30d=positive,
                    aftersale_30d=not positive,
                )
            )

        report = evaluate_baselines(
            examples,
            cutoff=datetime(2026, 7, 1, tzinfo=UTC8),
        )

        self.assertEqual(report["test_customer_count"], 120)
        self.assertEqual(report["excluded_seen_customer_count"], 1)
        self.assertEqual(set(report["baselines"]), {"rfm", "chat", "rfm_chat"})
        self.assertEqual(report["baselines"]["rfm_chat"]["top_k"][20]["precision"], 1.0)
        self.assertGreater(report["baselines"]["rfm_chat"]["top_k"][20]["lift"], 1.0)
        self.assertIn("pr_auc", report["baselines"]["rfm_chat"])
        self.assertIn("calibration", report["baselines"]["rfm_chat"])
        self.assertEqual(report["claim_mode"], "priority_correlation_only")

    def test_strategy_statistics_need_thirty_independent_customers(self) -> None:
        observations = [
            StrategyObservation(
                customer_key="customer-%02d" % index,
                exact_strategy="recommend:size",
                coarse_strategy="recommend",
                adopted=index % 2 == 0,
            )
            for index in range(29)
        ]
        first = select_strategy_statistics(observations, "recommend:size")
        self.assertEqual(first["strategy_key"], "recommend")
        self.assertFalse(first["exact_statistics_visible"])

        observations.append(
            StrategyObservation(
                customer_key="customer-29",
                exact_strategy="recommend:size",
                coarse_strategy="recommend",
                adopted=True,
            )
        )
        second = select_strategy_statistics(observations, "recommend:size")
        self.assertEqual(second["strategy_key"], "recommend:size")
        self.assertTrue(second["exact_statistics_visible"])


class PrivacyAndRolloutTests(unittest.TestCase):
    def test_blind_audit_flags_observed_action_pii_and_future_context(self) -> None:
        safe = {
            "card_id": "card-safe",
            "customer_key": "customer_0123456789abcdef",
            "as_of_at": "2026-07-01T10:00:00+08:00",
            "blind_context": [
                {
                    "role": "customer",
                    "text": "想了解这件商品",
                    "ended_at": "2026-07-01T10:00:00+08:00",
                }
            ],
        }
        unsafe = {
            **safe,
            "card_id": "card-unsafe",
            "phone": "13800138000",
            "observed_action": {"text": "未来回复"},
            "blind_context": [
                {
                    "role": "customer",
                    "text": "wxid_private",
                    "ended_at": "2026-07-02T10:00:00+08:00",
                }
            ],
        }
        audit = audit_blind_payloads([safe, unsafe])
        self.assertEqual(audit["audited"], 2)
        self.assertEqual(audit["safe"], 1)
        self.assertIn("observed_action_leak", audit["violations"])
        self.assertIn("pii_leak", audit["violations"])
        self.assertIn("future_context_leak", audit["violations"])

    def test_rollout_never_treats_implementation_as_human_release(self) -> None:
        shadow_days = {
            profile: tuple(date(2026, 7, 1) + timedelta(days=offset) for offset in range(14))
            for profile in ("aolai1", "aolai2", "aolai3", "aolai4")
        }
        evidence = {
            "m0_c_confirmed": False,
            "shadow_days_by_profile": shadow_days,
            "pilot_days": tuple(date(2026, 7, 15) + timedelta(days=offset) for offset in range(14)),
            "pilot_profile": "aolai1",
            "pilot_operator_count": 1,
            "critical_safety_incidents": 0,
            "stale_data_suggestions": 0,
            "draft_fact_errors": 0,
            "feedback_total": 100,
            "adopted_or_lightly_edited": 80,
        }
        blocked = evaluate_rollout(evidence)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("m0_c_human_confirmation_missing", blocked["blockers"])
        self.assertFalse(blocked["automatic_release"])

        evidence["m0_c_confirmed"] = True
        ready = evaluate_rollout(evidence)
        self.assertEqual(ready["status"], "awaiting_manual_release")
        self.assertEqual(ready["adoption_rate"], 0.8)
        self.assertFalse(ready["automatic_release"])


if __name__ == "__main__":
    unittest.main()
