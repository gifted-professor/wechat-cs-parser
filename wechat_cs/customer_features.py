"""Privacy-safe point-in-time customer features for the action queue.

This module is deliberately independent from SQLite and network I/O.  It only
accepts normalized M0 identities, orders and messages, then returns aggregate
features whose sole customer handle is an opaque ``customer_key``.  Phone HMACs
are used internally for joining and deduplication and are never present in the
returned dataclasses.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .core import redact_text


SHANGHAI = ZoneInfo("Asia/Shanghai")
FEATURE_RULE_VERSION = "customer-features-v2"
MESSAGE_FRESHNESS_SECONDS = 15 * 60
ORDER_FRESHNESS_SECONDS = 24 * 60 * 60
HEALTHY_COLLECTOR_STATES = frozenset({"running", "healthy", "ok"})

# Fixed monetary bands avoid cohort look-ahead.  Values are CNY minor units.
VALUE_LOW_UPPER_MINOR = 50_000
VALUE_MEDIUM_UPPER_MINOR = 200_000
VALUE_HIGH_UPPER_MINOR = 500_000

MIN_CONTACT_MESSAGES = 5
MIN_CONTACT_ACTIVE_DAYS = 3
MIN_CONTACT_WINDOW_SHARE = 0.40
MIN_RHYTHM_OBSERVATIONS = 5
MIN_RHYTHM_SHARE = 0.40
TURN_MERGE_SECONDS = 15 * 60
MAX_REPLY_DELAY = timedelta(days=7)
MANUAL_CONTACT_WINDOW = "工作时段人工选择"
_CONTACT_WINDOWS = (
    ("morning", 9, 12, "09:00-12:00"),
    ("afternoon", 12, 18, "12:00-18:00"),
    ("evening", 18, 22, "18:00-22:00"),
)
# Tie-break toward ordinary daytime hours before evening outreach.
_CONTACT_TIE_ORDER = {"afternoon": 0, "morning": 1, "evening": 2}
_OPAQUE_CUSTOMER = re.compile(r"^customer_[0-9a-f]{16,64}$")
_OPAQUE_PHONE = re.compile(r"^phone_[0-9a-f]{16,64}$")
_RHYTHM_PERIODS = (
    ("overnight", 0, 6),
    ("morning", 6, 12),
    ("noon", 12, 14),
    ("afternoon", 14, 18),
    ("evening", 18, 24),
)
_RHYTHM_TIE_ORDER = {
    "afternoon": 0,
    "morning": 1,
    "noon": 2,
    "evening": 3,
    "overnight": 4,
}


@dataclass(frozen=True)
class ApprovedIdentityLink:
    """An approved opaque conversation-to-phone link used only as input."""

    customer_key: str
    phone_hmac: str
    state: str = "approved"


@dataclass(frozen=True)
class SourceFreshness:
    as_of_at: str
    message_observed_until: Optional[str]
    order_synced_at: Optional[str]
    collector_status: str
    message_age_seconds: Optional[int]
    order_age_seconds: Optional[int]
    messages_fresh: bool
    orders_fresh: bool
    queue_ready: bool
    quality_flags: Tuple[str, ...]


@dataclass(frozen=True)
class FeatureBuildQuality:
    approved_identity_link_count: int
    excluded_identity_link_count: int
    identity_conflict_count: int
    deduplicated_phone_count: int
    invalid_message_count: int
    invalid_order_count: int


@dataclass(frozen=True)
class RhythmSummary:
    """Deterministic hour and month rhythm with an evidence threshold."""

    observation_count: int
    hour_counts: Tuple[int, ...]
    month_bucket_counts: Tuple[int, int, int]
    period_counts: Tuple[int, int, int, int, int]
    preferred_period: Optional[str]
    preference_state: str
    confidence: Optional[float]


@dataclass(frozen=True)
class CustomerProfile:
    customer_key: str
    as_of_at: str
    feature_rule_version: str
    linked_customer_count: int
    day_of_month_bucket: str

    customer_message_count: int
    active_day_count: int
    active_hour_counts: Tuple[int, ...]
    recommended_contact_window: str
    contact_window_basis: str
    contact_window_evidence_count: int
    contact_window_confidence: Optional[float]
    customer_message_rhythm: RhythmSummary
    customer_reply_rhythm: RhythmSummary
    reply_delay_observation_count: int
    median_reply_delay_seconds: Optional[float]

    order_features_available: bool
    rfm_recency_days: Optional[int]
    rfm_frequency: Optional[int]
    rfm_monetary_minor: Optional[int]
    value_bucket: str
    median_repurchase_interval_days: Optional[float]
    aftersales_count: Optional[int]
    aftersales_rate: Optional[float]
    aftersales_risk: str
    preferred_skus: Tuple[str, ...]
    preferred_factories: Tuple[str, ...]
    preferred_categories: Tuple[str, ...]
    preferred_colors: Tuple[str, ...]
    preferred_sizes: Tuple[str, ...]
    order_rhythm: RhythmSummary
    payment_rhythm: RhythmSummary

    unknown_aftersales_count: int
    quality_flags: Tuple[str, ...]


@dataclass(frozen=True)
class CustomerFeatureSnapshot:
    feature_rule_version: str
    freshness: SourceFreshness
    quality: FeatureBuildQuality
    profiles: Tuple[CustomerProfile, ...]


def _value(item: object, field: str, default: object = None) -> object:
    if isinstance(item, Mapping):
        return item.get(field, default)
    return getattr(item, field, default)


def _local_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("%s must be a valid ISO date or timestamp" % field) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _optional_local_datetime(value: object, *, field: str) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    return _local_datetime(value, field=field)


def _local_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        return _local_datetime(value, field=field).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().replace("/", "-")
    if not text:
        raise ValueError("%s must be a valid ISO date" % field)
    try:
        if "T" in text or " " in text:
            return _local_datetime(text, field=field).date()
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError("%s must be a valid ISO date" % field) from exc


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value is not None else None


def day_of_month_bucket(value: object) -> str:
    """Return the fixed 1-10 / 11-20 / 21-end-of-month bucket."""

    day = _local_date(value, field="day_of_month").day
    if day <= 10:
        return "early"
    if day <= 20:
        return "mid"
    return "late"


def _age_seconds(as_of_at: datetime, observed_at: Optional[datetime]) -> Optional[int]:
    if observed_at is None:
        return None
    return max(0, int((as_of_at - observed_at).total_seconds()))


def _freshness(
    *,
    as_of_at: datetime,
    message_observed_until: Optional[datetime],
    order_synced_at: Optional[datetime],
    collector_status: str,
    invalid_message_count: int,
) -> SourceFreshness:
    message_age = _age_seconds(as_of_at, message_observed_until)
    order_age = _age_seconds(as_of_at, order_synced_at)
    normalized_status = str(collector_status or "unknown").strip().lower() or "unknown"
    collector_healthy = normalized_status in HEALTHY_COLLECTOR_STATES
    messages_fresh = bool(
        collector_healthy
        and message_age is not None
        and message_age <= MESSAGE_FRESHNESS_SECONDS
        and invalid_message_count == 0
    )
    order_snapshot_after_as_of = bool(order_synced_at is not None and order_synced_at > as_of_at)
    orders_fresh = bool(
        not order_snapshot_after_as_of
        and order_age is not None
        and order_age <= ORDER_FRESHNESS_SECONDS
    )
    flags = set()
    if message_observed_until is None:
        flags.add("message_snapshot_missing")
    elif message_age is not None and message_age > MESSAGE_FRESHNESS_SECONDS:
        flags.add("message_snapshot_stale")
    if not collector_healthy:
        flags.add("collector_unhealthy")
    if invalid_message_count:
        flags.add("message_source_inconsistent")
    if order_synced_at is None:
        flags.add("order_snapshot_missing")
    elif order_snapshot_after_as_of:
        flags.add("order_snapshot_after_as_of")
    elif order_age is not None and order_age > ORDER_FRESHNESS_SECONDS:
        flags.add("order_snapshot_stale")
    return SourceFreshness(
        as_of_at=_iso(as_of_at) or "",
        message_observed_until=_iso(message_observed_until),
        order_synced_at=_iso(order_synced_at),
        collector_status=normalized_status,
        message_age_seconds=message_age,
        order_age_seconds=order_age,
        messages_fresh=messages_fresh,
        orders_fresh=orders_fresh,
        queue_ready=messages_fresh,
        quality_flags=tuple(sorted(flags)),
    )


def _opaque_identity(item: object) -> Tuple[str, str, str]:
    customer_key = str(_value(item, "customer_key") or "").strip()
    phone_hmac = str(_value(item, "phone_hmac") or "").strip()
    state = str(_value(item, "state", "approved") or "").strip().lower()
    if not _OPAQUE_CUSTOMER.fullmatch(customer_key) or not _OPAQUE_PHONE.fullmatch(phone_hmac):
        raise ValueError("identity links must use opaque customer_key and phone_hmac values")
    return customer_key, phone_hmac, state


def _choose_customer_key(
    customer_keys: Sequence[str],
    latest_activity: Mapping[str, Tuple[datetime, int, str]],
) -> str:
    available = [key for key in customer_keys if key in latest_activity]
    if not available:
        return min(customer_keys)
    latest = max(latest_activity[key] for key in available)
    return min(key for key in available if latest_activity[key] == latest)


def _contact_window(
    hour_counts: Sequence[int],
    *,
    message_count: int,
    active_day_count: int,
) -> Tuple[str, str, int, Optional[float]]:
    window_counts = []
    for code, start, end, label in _CONTACT_WINDOWS:
        window_counts.append((sum(hour_counts[start:end]), code, label))
    eligible_count = sum(item[0] for item in window_counts)
    if (
        message_count < MIN_CONTACT_MESSAGES
        or active_day_count < MIN_CONTACT_ACTIVE_DAYS
        or eligible_count < MIN_CONTACT_MESSAGES
    ):
        return MANUAL_CONTACT_WINDOW, "insufficient_evidence", eligible_count, None
    best_count, _best_code, best_label = min(
        window_counts,
        key=lambda item: (-item[0], _CONTACT_TIE_ORDER[item[1]]),
    )
    confidence = best_count / eligible_count if eligible_count else 0.0
    if best_count < 2 or confidence < MIN_CONTACT_WINDOW_SHARE:
        return MANUAL_CONTACT_WINDOW, "insufficient_evidence", eligible_count, None
    return best_label, "wechat_customer_messages", best_count, round(confidence, 6)


def _rhythm_summary(timestamps: Sequence[datetime]) -> RhythmSummary:
    hour_counts = [0] * 24
    month_counts = [0, 0, 0]
    for at in timestamps:
        hour_counts[at.hour] += 1
        bucket = day_of_month_bucket(at)
        month_counts[{"early": 0, "mid": 1, "late": 2}[bucket]] += 1
    period_counts = tuple(
        sum(hour_counts[start:end]) for _code, start, end in _RHYTHM_PERIODS
    )
    observation_count = len(timestamps)
    if observation_count < MIN_RHYTHM_OBSERVATIONS:
        return RhythmSummary(
            observation_count=observation_count,
            hour_counts=tuple(hour_counts),
            month_bucket_counts=tuple(month_counts),
            period_counts=period_counts,
            preferred_period=None,
            preference_state="insufficient_evidence",
            confidence=None,
        )
    ranked = sorted(
        zip(period_counts, (item[0] for item in _RHYTHM_PERIODS)),
        key=lambda item: (-item[0], _RHYTHM_TIE_ORDER[item[1]]),
    )
    best_count, best_period = ranked[0]
    confidence = best_count / observation_count if observation_count else 0.0
    if confidence < MIN_RHYTHM_SHARE:
        return RhythmSummary(
            observation_count=observation_count,
            hour_counts=tuple(hour_counts),
            month_bucket_counts=tuple(month_counts),
            period_counts=period_counts,
            preferred_period=None,
            preference_state="insufficient_evidence",
            confidence=None,
        )
    return RhythmSummary(
        observation_count=observation_count,
        hour_counts=tuple(hour_counts),
        month_bucket_counts=tuple(month_counts),
        period_counts=period_counts,
        preferred_period=best_period,
        preference_state="supported",
        confidence=round(confidence, 6),
    )


def _conversation_turns(
    rows: Sequence[Tuple[str, str, datetime, int, str]],
) -> Tuple[Tuple[str, datetime, datetime], ...]:
    """Merge consecutive same-role messages within a 15-minute turn."""

    turns = []
    for _customer_key, role, at, ordinal, message_key in sorted(
        rows, key=lambda row: (row[2], row[3], row[4])
    ):
        if (
            turns
            and turns[-1][0] == role
            and 0 <= (at - turns[-1][2]).total_seconds() <= TURN_MERGE_SECONDS
        ):
            previous_role, started_at, _ended_at = turns[-1]
            turns[-1] = (previous_role, started_at, at)
        else:
            turns.append((role, at, at))
    return tuple(turns)


def _reply_observations(
    turns: Sequence[Tuple[str, datetime, datetime]],
) -> Tuple[Tuple[datetime, float], ...]:
    """Match one customer reply to the latest preceding studio turn."""

    observations = []
    pending_studio_end: Optional[datetime] = None
    for role, started_at, ended_at in turns:
        if role == "studio":
            pending_studio_end = ended_at
            continue
        if pending_studio_end is None:
            continue
        delay = started_at - pending_studio_end
        if timedelta(0) <= delay <= MAX_REPLY_DELAY:
            observations.append((started_at, delay.total_seconds()))
        # Whether timely or late, this customer turn consumes the pending
        # studio turn so a later customer message cannot count twice.
        pending_studio_end = None
    return tuple(observations)


def _value_bucket(monetary_minor: int) -> str:
    if monetary_minor <= 0:
        return "none"
    if monetary_minor < VALUE_LOW_UPPER_MINOR:
        return "low"
    if monetary_minor < VALUE_MEDIUM_UPPER_MINOR:
        return "medium"
    if monetary_minor < VALUE_HIGH_UPPER_MINOR:
        return "high"
    return "vip"


def _safe_preference(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    redacted, _flags = redact_text(text)
    normalized = redacted.strip()
    return normalized[:200] or None


def _top_preferences(
    historical: Sequence[Tuple[date, int, object]], field: str, *, limit: int = 3
) -> Tuple[str, ...]:
    counts: Counter[str] = Counter()
    latest: Dict[str, date] = {}
    for paid_on, _revenue_minor, item in historical:
        value = _safe_preference(_value(item, field))
        if value:
            counts[value] += 1
            latest[value] = max(paid_on, latest.get(value, paid_on))
    ranked = sorted(
        counts,
        key=lambda value: (-counts[value], -latest[value].toordinal(), value),
    )
    return tuple(ranked[:limit])


def _profile_order_features(
    order_rows: Sequence[object],
    *,
    as_of_at: datetime,
    available: bool,
) -> Dict[str, Any]:
    unknown_aftersales = 0
    invalid_orders = 0
    historical = []
    order_timestamps = []
    payment_timestamps = []
    unknown_order_time_count = 0
    unknown_payment_time_count = 0

    empty_rhythm = _rhythm_summary(())

    if available:
        for item in order_rows:
            ordered_value = _value(item, "ordered_at")
            if ordered_value is None or str(ordered_value).strip() == "":
                continue
            try:
                ordered_at = _local_datetime(ordered_value, field="ordered_at")
            except ValueError:
                invalid_orders += 1
                continue
            if ordered_at <= as_of_at:
                if any(
                    (ordered_at.hour, ordered_at.minute, ordered_at.second, ordered_at.microsecond)
                ):
                    order_timestamps.append(ordered_at)
                else:
                    # Dashboard date columns are serialized as midnight even
                    # when no clock time was captured.  Retain the timestamp as
                    # an order fact, but never infer a midnight preference.
                    unknown_order_time_count += 1

    for item in order_rows:
        paid_value = _value(item, "paid_on")
        paid_at_value = _value(item, "paid_at")
        revenue_value = _value(item, "revenue_minor")
        if paid_value is None or str(paid_value).strip() == "":
            continue
        try:
            paid_on = _local_date(paid_value, field="paid_on")
            revenue_minor = int(revenue_value)
        except (TypeError, ValueError):
            invalid_orders += 1
            continue
        if revenue_minor <= 0:
            invalid_orders += 1
            continue
        paid_at = None
        if paid_at_value is not None and str(paid_at_value).strip() != "":
            try:
                paid_at = _local_datetime(paid_at_value, field="paid_at")
            except ValueError:
                invalid_orders += 1
                continue
            paid_on = paid_at.date()
            if any((paid_at.hour, paid_at.minute, paid_at.second, paid_at.microsecond)):
                if paid_at > as_of_at:
                    continue
            else:
                # Treat a zeroed clock as date-only evidence.  This avoids
                # leaking an unknown cutoff-day payment and avoids teaching a
                # false 00:00 buying habit.
                unknown_payment_time_count += 1
                if paid_on >= as_of_at.date():
                    continue
                paid_at = None
        else:
            # Legacy rows only have a calendar date. Exclude the cutoff day
            # conservatively because their exact payment time is unknowable.
            if paid_on >= as_of_at.date():
                continue
        historical.append((paid_on, revenue_minor, item))
        if paid_at is not None:
            payment_timestamps.append(paid_at)

    if not available:
        return {
            "rfm_recency_days": None,
            "rfm_frequency": None,
            "rfm_monetary_minor": None,
            "value_bucket": "unavailable",
            "median_repurchase_interval_days": None,
            "aftersales_count": None,
            "aftersales_rate": None,
            "aftersales_risk": "unavailable",
            "preferred_skus": (),
            "preferred_factories": (),
            "preferred_categories": (),
            "preferred_colors": (),
            "preferred_sizes": (),
            "order_rhythm": empty_rhythm,
            "payment_rhythm": empty_rhythm,
            "unknown_aftersales_count": 0,
            "unknown_order_time_count": unknown_order_time_count,
            "unknown_payment_time_count": unknown_payment_time_count,
            "invalid_order_count": invalid_orders,
        }

    payment_dates = sorted(item[0] for item in historical)
    distinct_payment_dates = sorted(set(payment_dates))
    intervals = [
        (later - earlier).days
        for earlier, later in zip(distinct_payment_dates, distinct_payment_dates[1:])
    ]
    monetary_minor = sum(item[1] for item in historical)
    aftersales_count = 0
    for _paid_on, _revenue_minor, item in historical:
        refund_type = str(_value(item, "refund_type") or "").strip().lower()
        if not refund_type:
            # Current return_status / aftersale_open is intentionally ignored:
            # it cannot be reconstructed safely for a historical as-of point.
            continue
        refund_value = _value(item, "refund_on")
        if refund_value is None or str(refund_value).strip() == "":
            unknown_aftersales += 1
            continue
        try:
            refund_on = _local_date(refund_value, field="refund_on")
        except ValueError:
            unknown_aftersales += 1
            continue
        if refund_on >= as_of_at.date():
            continue
        aftersales_count += 1

    frequency = len(historical)
    if frequency == 0:
        aftersales_rate = None
        aftersales_risk = "no_history"
    elif unknown_aftersales:
        aftersales_rate = None
        aftersales_risk = "unknown"
    else:
        aftersales_rate = aftersales_count / frequency
        if aftersales_rate == 0:
            aftersales_risk = "low"
        elif aftersales_rate < 0.30:
            aftersales_risk = "elevated"
        else:
            aftersales_risk = "high"

    return {
        "rfm_recency_days": (as_of_at.date() - max(payment_dates)).days if payment_dates else None,
        "rfm_frequency": frequency,
        "rfm_monetary_minor": monetary_minor,
        "value_bucket": _value_bucket(monetary_minor),
        "median_repurchase_interval_days": float(median(intervals)) if intervals else None,
        "aftersales_count": aftersales_count,
        "aftersales_rate": aftersales_rate,
        "aftersales_risk": aftersales_risk,
        "preferred_skus": _top_preferences(historical, "sku_name"),
        "preferred_factories": _top_preferences(historical, "factory"),
        "preferred_categories": _top_preferences(historical, "category"),
        "preferred_colors": _top_preferences(historical, "color"),
        "preferred_sizes": _top_preferences(historical, "size"),
        "order_rhythm": _rhythm_summary(order_timestamps),
        "payment_rhythm": _rhythm_summary(payment_timestamps),
        "unknown_aftersales_count": unknown_aftersales,
        "unknown_order_time_count": unknown_order_time_count,
        "unknown_payment_time_count": unknown_payment_time_count,
        "invalid_order_count": invalid_orders,
    }


def build_customer_profiles(
    identity_links: Sequence[object],
    orders: Sequence[object],
    messages: Sequence[object],
    *,
    as_of_at: object,
    message_observed_until: object,
    order_synced_at: object,
    collector_status: str,
) -> CustomerFeatureSnapshot:
    """Build one point-in-time profile per unique approved phone HMAC.

    Messages at or before ``as_of_at`` are eligible. Exact order timestamps are
    filtered against that boundary; legacy date-only payments on the cutoff day
    remain conservatively excluded. A stale order snapshot hides all
    value-derived fields, while a stale or unhealthy message source fails the
    queue closed.
    """

    as_of = _local_datetime(as_of_at, field="as_of_at")
    observed_until = _optional_local_datetime(
        message_observed_until, field="message_observed_until"
    )
    synced_at = _optional_local_datetime(order_synced_at, field="order_synced_at")

    approved_pairs = set()
    excluded_links = 0
    phones_by_customer: Dict[str, set] = defaultdict(set)
    for item in identity_links:
        customer_key, phone_hmac, state = _opaque_identity(item)
        if state != "approved":
            excluded_links += 1
            continue
        approved_pairs.add((customer_key, phone_hmac))
        phones_by_customer[customer_key].add(phone_hmac)

    conflicting_customers = {
        customer_key for customer_key, phone_hmacs in phones_by_customer.items() if len(phone_hmacs) > 1
    }
    phone_to_customers: Dict[str, set] = defaultdict(set)
    for customer_key, phone_hmac in approved_pairs:
        if customer_key in conflicting_customers:
            continue
        phone_to_customers[phone_hmac].add(customer_key)

    parsed_messages = []
    invalid_messages = 0
    latest_activity: Dict[str, Tuple[datetime, int, str]] = {}
    for item in messages:
        customer_key = str(_value(item, "customer_key") or "").strip()
        role = str(_value(item, "role") or "").strip().lower()
        message_key = str(_value(item, "message_key") or "").strip()
        try:
            ordinal = int(_value(item, "source_ordinal", 0))
            at = _local_datetime(_value(item, "timestamp"), field="message timestamp")
        except (TypeError, ValueError):
            invalid_messages += 1
            continue
        if role not in {"customer", "studio"} or not customer_key or not message_key:
            invalid_messages += 1
            continue
        # Rows after the historical cut-off must not influence even quality
        # metadata; otherwise adding a future message would mutate an old
        # point-in-time profile.
        if at <= as_of and observed_until is not None and at > observed_until:
            invalid_messages += 1
        if at > as_of:
            continue
        row = (customer_key, role, at, ordinal, message_key)
        parsed_messages.append(row)
        activity = (at, ordinal, message_key)
        if activity > latest_activity.get(customer_key, (datetime.min.replace(tzinfo=SHANGHAI), -1, "")):
            latest_activity[customer_key] = activity

    freshness = _freshness(
        as_of_at=as_of,
        message_observed_until=observed_until,
        order_synced_at=synced_at,
        collector_status=collector_status,
        invalid_message_count=invalid_messages,
    )

    orders_by_phone: Dict[str, list] = defaultdict(list)
    seen_orders = set()
    invalid_orders = 0
    for item in orders:
        phone_hmac = str(_value(item, "phone_hmac") or "").strip()
        order_id = str(_value(item, "order_line_id") or "").strip()
        if not phone_hmac or phone_hmac not in phone_to_customers:
            continue
        if not _OPAQUE_PHONE.fullmatch(phone_hmac) or not order_id:
            invalid_orders += 1
            continue
        key = (phone_hmac, order_id)
        if key in seen_orders:
            continue
        seen_orders.add(key)
        orders_by_phone[phone_hmac].append(item)

    profiles = []
    profile_invalid_orders = 0
    for phone_hmac, customer_key_set in sorted(phone_to_customers.items()):
        customer_keys = sorted(customer_key_set)
        primary_customer_key = _choose_customer_key(customer_keys, latest_activity)
        relevant_messages = [row for row in parsed_messages if row[0] in customer_key_set]
        customer_messages = [row for row in relevant_messages if row[1] == "customer"]
        hour_counts = [0] * 24
        active_dates = set()
        for _customer_key, _role, at, _ordinal, _message_key in customer_messages:
            hour_counts[at.hour] += 1
            active_dates.add(at.date())
        customer_turn_timestamps = []
        reply_observations = []
        for customer_key in customer_keys:
            conversation_rows = [row for row in relevant_messages if row[0] == customer_key]
            turns = _conversation_turns(conversation_rows)
            customer_turn_timestamps.extend(
                started_at for role, started_at, _ended_at in turns if role == "customer"
            )
            reply_observations.extend(_reply_observations(turns))
        customer_message_rhythm = _rhythm_summary(customer_turn_timestamps)
        reply_timestamps = [item[0] for item in reply_observations]
        reply_delays = [item[1] for item in reply_observations]
        customer_reply_rhythm = _rhythm_summary(reply_timestamps)

        reply_contact = _contact_window(
            customer_reply_rhythm.hour_counts,
            message_count=customer_reply_rhythm.observation_count,
            active_day_count=len({item.date() for item in reply_timestamps}),
        )
        if reply_contact[1] != "insufficient_evidence":
            contact_window = reply_contact[0]
            contact_basis = "wechat_customer_replies"
            evidence_count = reply_contact[2]
            contact_confidence = reply_contact[3]
        else:
            contact_window, contact_basis, evidence_count, contact_confidence = _contact_window(
                hour_counts,
                message_count=len(customer_messages),
                active_day_count=len(active_dates),
            )
        order_features = _profile_order_features(
            orders_by_phone.get(phone_hmac, ()),
            as_of_at=as_of,
            available=freshness.orders_fresh,
        )
        profile_invalid_orders += int(order_features.pop("invalid_order_count"))
        unknown_order_time_count = int(order_features.pop("unknown_order_time_count"))
        unknown_payment_time_count = int(order_features.pop("unknown_payment_time_count"))
        flags = set()
        if len(customer_keys) > 1:
            flags.add("linked_customer_deduplicated")
        if contact_basis == "insufficient_evidence":
            flags.add("insufficient_contact_evidence")
        if not freshness.orders_fresh:
            flags.add("order_features_unavailable")
        elif order_features["rfm_frequency"] == 0:
            flags.add("no_order_history")
        if order_features["unknown_aftersales_count"]:
            flags.add("incomplete_aftersales_history")
        if unknown_order_time_count:
            flags.add("order_clock_time_unknown")
        if unknown_payment_time_count:
            flags.add("payment_clock_time_unknown")

        profiles.append(
            CustomerProfile(
                customer_key=primary_customer_key,
                as_of_at=_iso(as_of) or "",
                feature_rule_version=FEATURE_RULE_VERSION,
                linked_customer_count=len(customer_keys),
                day_of_month_bucket=day_of_month_bucket(as_of),
                customer_message_count=len(customer_messages),
                active_day_count=len(active_dates),
                active_hour_counts=tuple(hour_counts),
                recommended_contact_window=contact_window,
                contact_window_basis=contact_basis,
                contact_window_evidence_count=evidence_count,
                contact_window_confidence=contact_confidence,
                customer_message_rhythm=customer_message_rhythm,
                customer_reply_rhythm=customer_reply_rhythm,
                reply_delay_observation_count=len(reply_delays),
                median_reply_delay_seconds=(
                    float(median(reply_delays)) if reply_delays else None
                ),
                order_features_available=freshness.orders_fresh,
                quality_flags=tuple(sorted(flags)),
                **order_features,
            )
        )

    quality = FeatureBuildQuality(
        approved_identity_link_count=len(approved_pairs) - sum(
            len(phones_by_customer[customer_key]) for customer_key in conflicting_customers
        ),
        excluded_identity_link_count=excluded_links,
        identity_conflict_count=len(conflicting_customers),
        deduplicated_phone_count=len(phone_to_customers),
        invalid_message_count=invalid_messages,
        invalid_order_count=invalid_orders + profile_invalid_orders,
    )
    return CustomerFeatureSnapshot(
        feature_rule_version=FEATURE_RULE_VERSION,
        freshness=freshness,
        quality=quality,
        profiles=tuple(sorted(profiles, key=lambda item: item.customer_key)),
    )
