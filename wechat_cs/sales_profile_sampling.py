"""Deterministic, mutually-exclusive sampling for the 50-person sales pilot."""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .core import hmac_id


SAMPLING_VERSION = "sales-profile-sampling-v2"
FULL_REMAINING_SAMPLING_VERSION = "sales-profile-full-remaining-v1"
DEFAULT_STRATUM_QUOTAS: Mapping[str, int] = OrderedDict(
    (
        ("complex_risk", 5),
        ("future_return_wait", 10),
        ("high_frequency", 10),
        ("high_value", 10),
        ("dormant_repeat", 10),
        ("control", 5),
    )
)
EXPANDED_100_STRATUM_QUOTAS: Mapping[str, int] = OrderedDict(
    (
        ("complex_risk", 10),
        ("future_return_wait", 10),
        ("high_frequency", 25),
        ("high_value", 25),
        ("dormant_repeat", 20),
        ("control", 10),
    )
)
BATCH_QUOTAS_BY_SIZE: Mapping[int, Mapping[str, int]] = {
    50: DEFAULT_STRATUM_QUOTAS,
    100: EXPANDED_100_STRATUM_QUOTAS,
}
REQUIRED_PROFILE_COUNT = 4
MIN_BIRTHDAY_MATCHES = 5


@dataclass(frozen=True)
class SamplingCandidate:
    customer_key: str
    profile_id: str
    phone_hmac: str
    feature_snapshot_id: Optional[str]
    complex_risk: bool
    future_return_wait: bool
    frequency: int
    monetary_minor: int
    average_order_minor: int
    recency_days: int
    aftersales_rate: Optional[float]
    future_signal_count: int
    birthday_match: bool


@dataclass(frozen=True)
class SelectedSubject:
    customer_key: str
    profile_id: str
    phone_hmac: str
    feature_snapshot_id: Optional[str]
    stratum: str
    stratum_rank: int
    birthday_match: bool
    selection_reason: Mapping[str, object]


def _stable_key(candidate: SamplingCandidate, secret: str) -> str:
    return hmac_id(
        secret,
        "sales-profile-sample",
        SAMPLING_VERSION,
        candidate.customer_key,
    )


def _eligible(candidate: SamplingCandidate, stratum: str) -> bool:
    if stratum == "complex_risk":
        return candidate.complex_risk
    if stratum == "future_return_wait":
        return candidate.future_return_wait
    if stratum == "high_frequency":
        return candidate.frequency >= 2
    if stratum == "high_value":
        return candidate.monetary_minor > 0
    if stratum == "dormant_repeat":
        return candidate.frequency >= 2 and candidate.recency_days >= 60
    if stratum == "control":
        return True
    raise ValueError("unknown sales profile stratum: %s" % stratum)


def _rank_key(
    candidate: SamplingCandidate,
    stratum: str,
    secret: str,
) -> Tuple[object, ...]:
    tie = _stable_key(candidate, secret)
    if stratum == "complex_risk":
        return (
            -(candidate.aftersales_rate or 0.0),
            -candidate.frequency,
            -candidate.monetary_minor,
            tie,
        )
    if stratum == "future_return_wait":
        return (-candidate.future_signal_count, -candidate.frequency, tie)
    if stratum == "high_frequency":
        return (-candidate.frequency, -candidate.monetary_minor, tie)
    if stratum == "high_value":
        return (-candidate.monetary_minor, -candidate.average_order_minor, tie)
    if stratum == "dormant_repeat":
        return (-candidate.recency_days, -candidate.frequency, tie)
    return (tie,)


def _reason(candidate: SamplingCandidate, stratum: str) -> Dict[str, object]:
    return {
        "sampling_version": SAMPLING_VERSION,
        "stratum": stratum,
        "complex_risk": candidate.complex_risk,
        "future_return_wait": candidate.future_return_wait,
        "frequency": candidate.frequency,
        "monetary_minor": candidate.monetary_minor,
        "average_order_minor": candidate.average_order_minor,
        "recency_days": candidate.recency_days,
        "aftersales_rate": candidate.aftersales_rate,
        "future_signal_count": candidate.future_signal_count,
        "birthday_match": candidate.birthday_match,
    }


def _repair_profile_coverage(
    selected: list[Tuple[str, SamplingCandidate]],
    candidates: Sequence[SamplingCandidate],
    *,
    secret: str,
    strata: Sequence[str],
) -> None:
    all_profiles = sorted({item.profile_id for item in candidates})
    if len(all_profiles) < REQUIRED_PROFILE_COUNT:
        raise ValueError("sampling candidates do not cover four profiles")
    selected_keys = {item.customer_key for _stratum, item in selected}
    selected_phones = {item.phone_hmac for _stratum, item in selected}
    while True:
        counts = Counter(item.profile_id for _stratum, item in selected)
        missing = [profile for profile in all_profiles if not counts[profile]]
        if not missing:
            return
        profile_id = missing[0]
        options = []
        for candidate in candidates:
            if candidate.profile_id != profile_id or candidate.customer_key in selected_keys:
                continue
            for stratum in strata:
                if not _eligible(candidate, stratum):
                    continue
                replaceable = [
                    (index, current)
                    for index, (current_stratum, current) in enumerate(selected)
                    if current_stratum == stratum
                    and counts[current.profile_id] > 1
                    and (
                        candidate.phone_hmac == current.phone_hmac
                        or candidate.phone_hmac not in selected_phones
                    )
                ]
                if replaceable:
                    options.append(
                        (
                            strata.index(stratum),
                            _rank_key(candidate, stratum, secret),
                            stratum,
                            candidate,
                            replaceable,
                        )
                    )
        if not options:
            raise ValueError("unable to guarantee four-profile sampling coverage")
        _precedence, _rank, stratum, replacement, replaceable = min(
            options, key=lambda item: (item[0], item[1])
        )
        replace_index, removed = max(
            replaceable,
            key=lambda item: _rank_key(item[1], stratum, secret),
        )
        selected[replace_index] = (stratum, replacement)
        selected_keys.remove(removed.customer_key)
        selected_keys.add(replacement.customer_key)
        selected_phones.remove(removed.phone_hmac)
        selected_phones.add(replacement.phone_hmac)


def _repair_birthday_coverage(
    selected: list[Tuple[str, SamplingCandidate]],
    candidates: Sequence[SamplingCandidate],
    *,
    secret: str,
    strata: Sequence[str],
    minimum_birthday_matches: int,
) -> None:
    selected_keys = {item.customer_key for _stratum, item in selected}
    selected_phones = {item.phone_hmac for _stratum, item in selected}
    while sum(item.birthday_match for _stratum, item in selected) < minimum_birthday_matches:
        profile_counts = Counter(item.profile_id for _stratum, item in selected)
        options = []
        for replacement in candidates:
            if not replacement.birthday_match or replacement.customer_key in selected_keys:
                continue
            for stratum in strata:
                if not _eligible(replacement, stratum):
                    continue
                replaceable = [
                    (index, current)
                    for index, (current_stratum, current) in enumerate(selected)
                    if current_stratum == stratum
                    and not current.birthday_match
                    and (
                        current.profile_id == replacement.profile_id
                        or profile_counts[current.profile_id] > 1
                    )
                    and (
                        replacement.phone_hmac == current.phone_hmac
                        or replacement.phone_hmac not in selected_phones
                    )
                ]
                if replaceable:
                    options.append(
                        (
                            strata.index(stratum),
                            _rank_key(replacement, stratum, secret),
                            stratum,
                            replacement,
                            replaceable,
                        )
                    )
        if not options:
            raise ValueError("unable to guarantee five birthday-linked subjects")
        _precedence, _rank, stratum, replacement, replaceable = min(
            options, key=lambda item: (item[0], item[1])
        )
        replace_index, removed = max(
            replaceable,
            key=lambda item: _rank_key(item[1], stratum, secret),
        )
        selected[replace_index] = (stratum, replacement)
        selected_keys.remove(removed.customer_key)
        selected_keys.add(replacement.customer_key)
        selected_phones.remove(removed.phone_hmac)
        selected_phones.add(replacement.phone_hmac)


def select_sales_profile_subjects(
    candidates: Iterable[SamplingCandidate],
    *,
    secret: str,
    quotas: Mapping[str, int] = DEFAULT_STRATUM_QUOTAS,
    excluded_customer_keys: Sequence[str] = (),
    excluded_phone_hmacs: Sequence[str] = (),
    minimum_birthday_matches: int = MIN_BIRTHDAY_MATCHES,
) -> Tuple[SelectedSubject, ...]:
    """Select a frozen, stable, mutually-exclusive pilot cohort."""

    if tuple(quotas) != tuple(DEFAULT_STRATUM_QUOTAS):
        raise ValueError("sales profile strata must retain the approved precedence")
    if any(int(quota) < 1 for quota in quotas.values()):
        raise ValueError("sales profile stratum quotas must be positive")
    if minimum_birthday_matches < 0:
        raise ValueError("minimum_birthday_matches must not be negative")
    excluded = frozenset(str(item) for item in excluded_customer_keys)
    excluded_phones = frozenset(str(item) for item in excluded_phone_hmacs)
    rows = tuple(
        item
        for item in candidates
        if item.customer_key not in excluded and item.phone_hmac not in excluded_phones
    )
    if len({item.customer_key for item in rows}) != len(rows):
        raise ValueError("sampling candidates must have unique customer keys")
    selected: list[Tuple[str, SamplingCandidate]] = []
    selected_keys = set()
    selected_phones = set()
    for stratum, quota in quotas.items():
        ranked = sorted(
            (
                item
                for item in rows
                if item.customer_key not in selected_keys and _eligible(item, stratum)
            ),
            key=lambda item: _rank_key(item, stratum, secret),
        )
        chosen = []
        for item in ranked:
            if item.phone_hmac in selected_phones:
                continue
            chosen.append(item)
            selected_phones.add(item.phone_hmac)
            if len(chosen) == quota:
                break
        if len(chosen) < quota:
            raise ValueError(
                "insufficient eligible candidates for %s: need %d, got %d"
                % (stratum, quota, len(chosen))
            )
        selected.extend((stratum, item) for item in chosen)
        selected_keys.update(item.customer_key for item in chosen)

    strata = tuple(quotas)
    _repair_profile_coverage(selected, rows, secret=secret, strata=strata)
    _repair_birthday_coverage(
        selected,
        rows,
        secret=secret,
        strata=strata,
        minimum_birthday_matches=minimum_birthday_matches,
    )
    if len({item.phone_hmac for _stratum, item in selected}) != len(selected):
        raise RuntimeError("sales profile sampling produced a duplicate person")

    output = []
    for stratum in quotas:
        ranked = sorted(
            (item for selected_stratum, item in selected if selected_stratum == stratum),
            key=lambda item: _rank_key(item, stratum, secret),
        )
        for rank, item in enumerate(ranked, start=1):
            output.append(
                SelectedSubject(
                    customer_key=item.customer_key,
                    profile_id=item.profile_id,
                    phone_hmac=item.phone_hmac,
                    feature_snapshot_id=item.feature_snapshot_id,
                    stratum=stratum,
                    stratum_rank=rank,
                    birthday_match=item.birthday_match,
                    selection_reason=_reason(item, stratum),
                )
            )
    return tuple(output)


def select_all_remaining_sales_profile_subjects(
    candidates: Iterable[SamplingCandidate],
    *,
    secret: str,
    excluded_customer_keys: Sequence[str] = (),
    excluded_phone_hmacs: Sequence[str] = (),
) -> Tuple[SelectedSubject, ...]:
    """Freeze every remaining eligible person once, deduplicated by phone HMAC."""

    excluded = frozenset(str(item) for item in excluded_customer_keys)
    excluded_phones = frozenset(str(item) for item in excluded_phone_hmacs)
    rows = tuple(
        item
        for item in candidates
        if item.customer_key not in excluded and item.phone_hmac not in excluded_phones
    )
    if len({item.customer_key for item in rows}) != len(rows):
        raise ValueError("sampling candidates must have unique customer keys")

    def rank(item: SamplingCandidate) -> Tuple[object, ...]:
        return (
            item.recency_days,
            -item.frequency,
            -item.monetary_minor,
            _stable_key(item, secret),
        )

    by_phone: Dict[str, SamplingCandidate] = {}
    for item in rows:
        current = by_phone.get(item.phone_hmac)
        if current is None or rank(item) < rank(current):
            by_phone[item.phone_hmac] = item
    selected = sorted(by_phone.values(), key=rank)
    strata = (
        "complex_risk",
        "future_return_wait",
        "dormant_repeat",
        "high_frequency",
        "control",
    )
    grouped: Dict[str, list[SamplingCandidate]] = {stratum: [] for stratum in strata}
    for item in selected:
        if item.complex_risk:
            stratum = "complex_risk"
        elif item.future_return_wait:
            stratum = "future_return_wait"
        elif item.frequency >= 2 and item.recency_days >= 60:
            stratum = "dormant_repeat"
        elif item.frequency >= 2:
            stratum = "high_frequency"
        else:
            stratum = "control"
        grouped[stratum].append(item)
    output = []
    for stratum in strata:
        for index, item in enumerate(sorted(grouped[stratum], key=rank), start=1):
            reason = _reason(item, stratum)
            reason["sampling_version"] = FULL_REMAINING_SAMPLING_VERSION
            reason["cohort_mode"] = "full_remaining"
            reason["phone_deduplicated"] = True
            output.append(
                SelectedSubject(
                    customer_key=item.customer_key,
                    profile_id=item.profile_id,
                    phone_hmac=item.phone_hmac,
                    feature_snapshot_id=item.feature_snapshot_id,
                    stratum=stratum,
                    stratum_rank=index,
                    birthday_match=item.birthday_match,
                    selection_reason=reason,
                )
            )
    return tuple(output)


__all__ = [
    "BATCH_QUOTAS_BY_SIZE",
    "DEFAULT_STRATUM_QUOTAS",
    "EXPANDED_100_STRATUM_QUOTAS",
    "FULL_REMAINING_SAMPLING_VERSION",
    "MIN_BIRTHDAY_MATCHES",
    "SAMPLING_VERSION",
    "SamplingCandidate",
    "SelectedSubject",
    "select_sales_profile_subjects",
    "select_all_remaining_sales_profile_subjects",
]
