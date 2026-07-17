"""Offline evaluation, privacy auditing, and manual rollout gates.

The functions in this module are deliberately read-only and model-free.  They
measure historical ranking correlation and rollout evidence; they never claim
incremental causality and never authorize a production release.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


BASELINES = ("rfm", "chat", "rfm_chat")
TOP_KS = (20, 50, 100)
REQUIRED_SHADOW_PROFILES = frozenset({"aolai1", "aolai2", "aolai3", "aolai4"})

_SENSITIVE_KEY = re.compile(
    r"(?:observed_action|action_message|card_outcome|outcomes?|phone|mobile|wxid|"
    r"wechat(?:_?id)?|raw_?id|hmac|source_file)",
    re.IGNORECASE,
)
_PII_TEXT = re.compile(r"(?:\b1[3-9]\d{9}\b|wxid_[A-Za-z0-9_-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ScoredExample:
    customer_key: str
    as_of_at: datetime
    paid_7d: Optional[bool]
    rfm_score: float
    chat_score: float
    combined_score: float
    retained_30d: Optional[bool] = None
    aftersale_30d: Optional[bool] = None


@dataclass(frozen=True)
class StrategyObservation:
    customer_key: str
    exact_strategy: str
    coarse_strategy: str
    adopted: bool


def _validate_example(item: ScoredExample) -> None:
    if not item.customer_key:
        raise ValueError("customer_key is required")
    if item.as_of_at.tzinfo is None or item.as_of_at.utcoffset() is None:
        raise ValueError("as_of_at must include a timezone")
    for name, value in (
        ("rfm_score", item.rfm_score),
        ("chat_score", item.chat_score),
        ("combined_score", item.combined_score),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s must be numeric" % name)
        if not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
            raise ValueError("%s must be between 0 and 1" % name)


def _average_precision(rows: Sequence[Tuple[float, bool]]) -> Optional[float]:
    positives = sum(1 for _score, label in rows if label)
    if positives == 0:
        return None
    found = 0
    precision_sum = 0.0
    for rank, (_score, label) in enumerate(rows, start=1):
        if label:
            found += 1
            precision_sum += found / rank
    return precision_sum / positives


def _calibration(rows: Sequence[Tuple[float, bool]]) -> Dict[str, Optional[float]]:
    if not rows:
        return {"brier": None, "expected_calibration_error": None}
    brier = sum((score - int(label)) ** 2 for score, label in rows) / len(rows)
    bins: Dict[int, list] = defaultdict(list)
    for score, label in rows:
        index = min(9, int(score * 10))
        bins[index].append((score, label))
    ece = 0.0
    for values in bins.values():
        confidence = sum(item[0] for item in values) / len(values)
        observed = sum(int(item[1]) for item in values) / len(values)
        ece += (len(values) / len(rows)) * abs(confidence - observed)
    return {
        "brier": round(brier, 6),
        "expected_calibration_error": round(ece, 6),
    }


def _optional_rate(values: Iterable[Optional[bool]]) -> Optional[float]:
    known = [bool(value) for value in values if value is not None]
    return round(sum(known) / len(known), 6) if known else None


def _baseline_report(
    examples: Sequence[ScoredExample], score_field: str
) -> Dict[str, Any]:
    labeled = [item for item in examples if item.paid_7d is not None]
    ranked = sorted(
        labeled,
        key=lambda item: (-float(getattr(item, score_field)), item.customer_key),
    )
    scored_labels = [
        (float(getattr(item, score_field)), bool(item.paid_7d)) for item in ranked
    ]
    base_rate = _optional_rate(item.paid_7d for item in labeled)
    top_k: Dict[int, Dict[str, Optional[float]]] = {}
    for requested in TOP_KS:
        selected = ranked[:requested]
        precision = _optional_rate(item.paid_7d for item in selected)
        lift = (
            round(precision / base_rate, 6)
            if precision is not None and base_rate not in (None, 0)
            else None
        )
        top_k[requested] = {
            "evaluated": len(selected),
            "precision": precision,
            "lift": lift,
            "retained_30d_rate": _optional_rate(
                item.retained_30d for item in selected
            ),
            "aftersale_30d_rate": _optional_rate(
                item.aftersale_30d for item in selected
            ),
        }
    return {
        "labeled_customer_count": len(labeled),
        "positive_rate": base_rate,
        "pr_auc": (
            round(value, 6) if (value := _average_precision(scored_labels)) is not None else None
        ),
        "calibration": _calibration(scored_labels),
        "top_k": top_k,
    }


def evaluate_baselines(
    examples: Sequence[ScoredExample], *, cutoff: datetime
) -> Dict[str, Any]:
    """Compare RFM, chat, and combined scores on unseen future customers.

    Rows at or before ``cutoff`` define the training-customer set.  A customer
    seen there is excluded from the later test set, and only one latest future
    row per unseen customer is retained.  This is a ranking association report,
    not evidence that a reply or contact time caused a sale.
    """

    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must include a timezone")
    for item in examples:
        _validate_example(item)
    train_customers = {
        item.customer_key for item in examples if item.as_of_at <= cutoff
    }
    future = [item for item in examples if item.as_of_at > cutoff]
    excluded = {item.customer_key for item in future if item.customer_key in train_customers}
    latest_by_customer: Dict[str, ScoredExample] = {}
    for item in future:
        if item.customer_key in train_customers:
            continue
        current = latest_by_customer.get(item.customer_key)
        if current is None or item.as_of_at > current.as_of_at:
            latest_by_customer[item.customer_key] = item
    test = tuple(latest_by_customer[key] for key in sorted(latest_by_customer))
    return {
        "cutoff": cutoff.isoformat(),
        "train_customer_count": len(train_customers),
        "test_customer_count": len(test),
        "excluded_seen_customer_count": len(excluded),
        "customer_level_isolation": True,
        "time_out_test": True,
        "claim_mode": "priority_correlation_only",
        "baselines": {
            "rfm": _baseline_report(test, "rfm_score"),
            "chat": _baseline_report(test, "chat_score"),
            "rfm_chat": _baseline_report(test, "combined_score"),
        },
    }


def select_strategy_statistics(
    observations: Sequence[StrategyObservation],
    exact_strategy: str,
    *,
    minimum_customers: int = 30,
) -> Dict[str, Any]:
    """Show exact strategy statistics only after 30 independent customers."""

    if minimum_customers < 1:
        raise ValueError("minimum_customers must be positive")
    matching = [item for item in observations if item.exact_strategy == exact_strategy]
    if not matching:
        return {
            "strategy_key": None,
            "independent_customer_count": 0,
            "adoption_rate": None,
            "exact_statistics_visible": False,
        }
    coarse = sorted({item.coarse_strategy for item in matching})
    if len(coarse) != 1:
        raise ValueError("one exact strategy must map to one coarse strategy")
    exact_by_customer = {item.customer_key: item for item in matching}
    show_exact = len(exact_by_customer) >= minimum_customers
    if show_exact:
        selected = exact_by_customer
        strategy_key = exact_strategy
    else:
        selected = {
            item.customer_key: item
            for item in observations
            if item.coarse_strategy == coarse[0]
        }
        strategy_key = coarse[0]
    values = list(selected.values())
    return {
        "strategy_key": strategy_key,
        "independent_customer_count": len(values),
        "adoption_rate": round(sum(item.adopted for item in values) / len(values), 6)
        if values
        else None,
        "exact_statistics_visible": show_exact,
        "minimum_independent_customers": minimum_customers,
    }


def _walk(value: Any, *, keys: list, strings: list) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.append(str(key))
            _walk(item, keys=keys, strings=strings)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, keys=keys, strings=strings)
    elif isinstance(value, str):
        strings.append(value)


def _parse_timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def audit_blind_payloads(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Audit model-facing decision payloads for future/action/PII leakage."""

    violation_counts: Counter[str] = Counter()
    unsafe_cards = 0
    for payload in payloads:
        card_violations = set()
        keys: list = []
        strings: list = []
        _walk(payload, keys=keys, strings=strings)
        if any(
            re.search(r"(?:observed_action|action_message|card_outcome|outcomes?)", key, re.I)
            for key in keys
        ):
            card_violations.add("observed_action_leak")
        if any(_SENSITIVE_KEY.search(key) for key in keys) or any(
            _PII_TEXT.search(value) for value in strings
        ):
            card_violations.add("pii_leak")
        as_of = _parse_timestamp(payload.get("as_of_at"))
        context = payload.get("blind_context")
        if as_of is not None and isinstance(context, Sequence):
            for turn in context:
                if not isinstance(turn, Mapping):
                    continue
                for field in ("started_at", "ended_at", "timestamp"):
                    moment = _parse_timestamp(turn.get(field))
                    if moment is not None and moment > as_of:
                        card_violations.add("future_context_leak")
        if card_violations:
            unsafe_cards += 1
            violation_counts.update(card_violations)
    return {
        "audited": len(payloads),
        "safe": len(payloads) - unsafe_cards,
        "unsafe": unsafe_cards,
        "violations": dict(sorted(violation_counts.items())),
        "passed": unsafe_cards == 0,
    }


def _distinct_days(values: object) -> set:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    output = set()
    for value in values:
        if isinstance(value, datetime):
            output.add(value.date())
        elif isinstance(value, date):
            output.add(value)
        else:
            try:
                output.add(date.fromisoformat(str(value)[:10]))
            except ValueError:
                continue
    return output


def evaluate_rollout(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate shadow/pilot evidence while retaining a human release gate."""

    blockers = []
    if evidence.get("m0_c_confirmed") is not True:
        blockers.append("m0_c_human_confirmation_missing")

    shadow = evidence.get("shadow_days_by_profile")
    if not isinstance(shadow, Mapping):
        shadow = {}
    for profile in sorted(REQUIRED_SHADOW_PROFILES):
        if len(_distinct_days(shadow.get(profile))) < 14:
            blockers.append("shadow_14_days_incomplete:%s" % profile)

    if evidence.get("pilot_profile") != "aolai1":
        blockers.append("pilot_profile_must_be_aolai1")
    if int(evidence.get("pilot_operator_count") or 0) != 1:
        blockers.append("pilot_operator_count_must_be_one")
    if len(_distinct_days(evidence.get("pilot_days"))) < 14:
        blockers.append("pilot_14_days_incomplete")

    for field, blocker in (
        ("critical_safety_incidents", "critical_safety_incident"),
        ("stale_data_suggestions", "stale_data_suggestion"),
        ("draft_fact_errors", "draft_fact_error"),
    ):
        if int(evidence.get(field) or 0) != 0:
            blockers.append(blocker)

    feedback_total = int(evidence.get("feedback_total") or 0)
    adopted = int(evidence.get("adopted_or_lightly_edited") or 0)
    adoption_rate = adopted / feedback_total if feedback_total > 0 else None
    if adoption_rate is None or adoption_rate < 0.70:
        blockers.append("adoption_rate_below_70_percent")

    status = "blocked" if blockers else "awaiting_manual_release"
    return {
        "status": status,
        "blockers": blockers,
        "adoption_rate": round(adoption_rate, 6) if adoption_rate is not None else None,
        "automatic_release": False,
        "manual_release_required": True,
        "causal_claim_allowed": False,
    }


__all__ = [
    "BASELINES",
    "ScoredExample",
    "StrategyObservation",
    "audit_blind_payloads",
    "evaluate_baselines",
    "evaluate_rollout",
    "select_strategy_statistics",
]
