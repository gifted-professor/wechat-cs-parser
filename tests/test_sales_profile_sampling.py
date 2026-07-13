from __future__ import annotations

import unittest
from dataclasses import replace

from wechat_cs.sales_profile_sampling import (
    DEFAULT_STRATUM_QUOTAS,
    SamplingCandidate,
    select_sales_profile_subjects,
)


SECRET = "sampling-fixture-secret-with-at-least-32-characters"
PROFILES = ("aolai1", "aolai2", "aolai4", "service")


def candidate(
    number: int,
    *,
    profile_id: str = "aolai1",
    complex_risk: bool = False,
    future_return_wait: bool = False,
    frequency: int = 1,
    monetary_minor: int = 10_000,
    recency_days: int = 10,
    birthday_match: bool = False,
) -> SamplingCandidate:
    return SamplingCandidate(
        customer_key="customer_%024x" % number,
        profile_id=profile_id,
        phone_hmac="phone_%024x" % number,
        feature_snapshot_id="feature-%d" % number,
        complex_risk=complex_risk,
        future_return_wait=future_return_wait,
        frequency=frequency,
        monetary_minor=monetary_minor,
        average_order_minor=(monetary_minor // frequency if frequency else 0),
        recency_days=recency_days,
        aftersales_rate=0.5 if complex_risk else 0.0,
        future_signal_count=2 if future_return_wait else 0,
        birthday_match=birthday_match,
    )


def rich_candidate_pool() -> list[SamplingCandidate]:
    rows: list[SamplingCandidate] = []
    number = 1
    # Extra candidates in every stratum leave room for deterministic coverage
    # swaps without changing the fixed quotas.
    for index in range(9):
        rows.append(
            candidate(
                number,
                profile_id=PROFILES[index % 4],
                complex_risk=True,
                frequency=20 - index,
                monetary_minor=500_000 - index * 1_000,
                birthday_match=index < 2,
            )
        )
        number += 1
    for index in range(15):
        rows.append(
            candidate(
                number,
                profile_id=PROFILES[index % 4],
                future_return_wait=True,
                frequency=12 - min(index, 10),
                monetary_minor=300_000 - index * 1_000,
                birthday_match=index < 2,
            )
        )
        number += 1
    for index in range(16):
        rows.append(
            candidate(
                number,
                profile_id=PROFILES[index % 4],
                frequency=50 - index,
                monetary_minor=200_000 + index,
                birthday_match=index == 0,
            )
        )
        number += 1
    for index in range(16):
        rows.append(
            candidate(
                number,
                profile_id=PROFILES[index % 4],
                frequency=2,
                monetary_minor=2_000_000 - index * 10_000,
                birthday_match=index == 0,
            )
        )
        number += 1
    for index in range(16):
        rows.append(
            candidate(
                number,
                profile_id=PROFILES[index % 4],
                frequency=3 + index % 3,
                monetary_minor=80_000 + index,
                recency_days=365 - index,
                birthday_match=index == 0,
            )
        )
        number += 1
    for index in range(12):
        rows.append(
            candidate(
                number,
                profile_id=PROFILES[index % 4],
                frequency=1,
                monetary_minor=20_000 + index,
                recency_days=20 + index,
            )
        )
        number += 1
    return rows


class SalesProfileSamplingTests(unittest.TestCase):
    def test_exact_mutually_exclusive_quota_and_stable_rerun(self) -> None:
        pool = rich_candidate_pool()
        first = select_sales_profile_subjects(pool, secret=SECRET)
        second = select_sales_profile_subjects(list(reversed(pool)), secret=SECRET)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 50)
        self.assertEqual(len({item.customer_key for item in first}), 50)
        self.assertEqual(len({item.phone_hmac for item in first}), 50)
        counts = {
            stratum: sum(item.stratum == stratum for item in first)
            for stratum in DEFAULT_STRATUM_QUOTAS
        }
        self.assertEqual(counts, dict(DEFAULT_STRATUM_QUOTAS))
        for stratum, quota in DEFAULT_STRATUM_QUOTAS.items():
            self.assertEqual(
                [item.stratum_rank for item in first if item.stratum == stratum],
                list(range(1, quota + 1)),
            )

    def test_account_and_birthday_coverage_are_repaired_within_strata(self) -> None:
        selected = select_sales_profile_subjects(rich_candidate_pool(), secret=SECRET)
        self.assertEqual({item.profile_id for item in selected}, set(PROFILES))
        self.assertGreaterEqual(sum(item.birthday_match for item in selected), 5)

    def test_precedence_prevents_future_candidate_from_entering_second_stratum_twice(self) -> None:
        pool = rich_candidate_pool()
        both = candidate(
            999,
            complex_risk=True,
            future_return_wait=True,
            frequency=999,
            monetary_minor=9_000_000,
            birthday_match=True,
        )
        selected = select_sales_profile_subjects([both, *pool], secret=SECRET)
        match = [item for item in selected if item.customer_key == both.customer_key]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].stratum, "complex_risk")

    def test_insufficient_stratum_fails_before_freezing_partial_cohort(self) -> None:
        pool = [
            candidate(index, frequency=1, recency_days=1)
            for index in range(1, 80)
        ]
        with self.assertRaisesRegex(ValueError, "complex_risk"):
            select_sales_profile_subjects(pool, secret=SECRET)

    def test_same_phone_across_accounts_is_selected_as_one_person(self) -> None:
        pool = rich_candidate_pool()
        original = pool[0]
        duplicate = replace(
            original,
            customer_key="customer_%024x" % 9999,
            profile_id="service",
            feature_snapshot_id="feature-duplicate-phone",
            frequency=999,
            monetary_minor=99_000_000,
        )
        selected = select_sales_profile_subjects([duplicate, *pool], secret=SECRET)
        shared = [item for item in selected if item.phone_hmac == original.phone_hmac]
        self.assertEqual(len(shared), 1)
        self.assertEqual(len({item.phone_hmac for item in selected}), 50)


if __name__ == "__main__":
    unittest.main()
