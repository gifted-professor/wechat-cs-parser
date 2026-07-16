"""Read-only conversion attribution audit for historical contact episodes.

The audit deliberately separates order-centric attribution from episode-centric
learning samples.  It never mutates the normalized database, never trains a
weight, and never claims that a contact or reply caused a purchase.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .core import json_dumps, parse_timestamp
from .source_snapshot import assert_project_output
from .store import open_store


SHANGHAI = ZoneInfo("Asia/Shanghai")
AUDIT_VERSION = "conversion-attribution-audit-v1"
ATTRIBUTION_STATES = (
    "high_confidence_cross_day",
    "same_day_correlation",
    "competing_contact_episodes",
    "no_matching_contact",
    "identity_unverified",
    "quality_unknown",
)

_AFTERSALES_MARKERS = (
    "售后",
    "退款",
    "退货",
    "换货",
    "补发",
    "少发",
    "漏发",
    "错发",
    "破损",
    "坏了",
    "物流",
    "快递",
    "发货",
    "单号",
    "签收",
    "到货",
)
_SALES_INQUIRY_MARKERS = (
    "多少钱",
    "价格",
    "报价",
    "怎么买",
    "怎么拍",
    "下单",
    "链接",
    "想要",
    "想买",
    "有货",
    "现货",
    "库存",
    "推荐",
    "适合",
    "尺码",
    "尺寸",
    "颜色",
    "款式",
    "活动",
    "优惠",
    "折扣",
    "便宜",
    "贵",
)
_EXPLICIT_PRICE_OBJECTION_MARKERS = (
    "太贵",
    "有点贵",
    "价格高",
    "价高",
    "贵了",
    "贵呢",
    "超预算",
    "超过预算",
    "预算不够",
    "预算有限",
    "买不起",
)
_PROMOTION_WAIT_MARKERS = (
    "等活动",
    "有活动再",
    "活动再买",
    "打折再",
    "优惠再",
    "降价再",
)
_DISCOUNT_REQUEST_MARKERS = (
    "便宜点",
    "便宜一点",
    "能优惠",
    "有优惠吗",
    "最低多少",
    "最低价",
    "少一点",
    "打折吗",
    "折扣吗",
)
_QUALITY_BLOCKING_FLAGS = frozenset(
    {
        "invalid_paid_on",
        "invalid_revenue",
        "future_paid_on",
        "missing_refund_on",
        "missing_refund_amount",
        "invalid_refund_on",
        "invalid_refund_amount",
        "refund_exceeds_revenue",
        "future_refund_on",
        "aftersale_open",
    }
)


@dataclass(frozen=True)
class ContactEpisode:
    customer_key: str
    episode_id: str
    origin: str
    started_at: datetime
    ended_at: datetime
    intent: str
    explicit_price_barrier: str
    suspected_barrier: str
    talk_track_tags: Tuple[str, ...]
    talk_track_primary: str
    card_count: int


@dataclass(frozen=True)
class PurchaseEvent:
    purchase_event_id: str
    phone_hmac: str
    paid_on: date
    source_record_count: int
    gross_revenue_minor: int
    net_revenue_minor: int
    quality_flags: Tuple[str, ...]
    prior_purchase_count: int = 0
    repeat_30d: Optional[bool] = None
    repeat_60d: Optional[bool] = None
    repeat_90d: Optional[bool] = None


@dataclass(frozen=True)
class AttributionDecision:
    purchase_event_id: str
    attribution_state: str
    customer_key: Optional[str]
    episode_ids: Tuple[str, ...]
    contact_origin: Optional[str]
    intent: Optional[str]
    explicit_price_barrier: Optional[str]
    suspected_barrier: Optional[str]
    talk_track_primary: Optional[str]
    days_from_contact: Optional[int]
    eligible_for_method_learning: bool
    reason_codes: Tuple[str, ...]


def _moment(value: str) -> datetime:
    parsed = parse_timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _json(value: object, default: object) -> object:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _contains(text: str, markers: Iterable[str]) -> bool:
    normalized = str(text or "").replace(" ", "")
    return any(marker in normalized for marker in markers)


def classify_customer_signal(text: str) -> Dict[str, str]:
    """Return deterministic intent and explicit price-barrier labels."""

    if _contains(text, _AFTERSALES_MARKERS):
        intent = "aftersales_or_order_status"
    elif _contains(text, _SALES_INQUIRY_MARKERS):
        intent = "sales_inquiry"
    else:
        intent = "general_or_unknown"

    if _contains(text, _EXPLICIT_PRICE_OBJECTION_MARKERS):
        barrier = "explicit_price_objection"
    elif _contains(text, _PROMOTION_WAIT_MARKERS):
        barrier = "promotion_wait"
    elif _contains(text, _DISCOUNT_REQUEST_MARKERS):
        barrier = "discount_request"
    else:
        barrier = "none"
    return {"intent": intent, "explicit_price_barrier": barrier}


def classify_actual_talk_track(text: str) -> Tuple[str, ...]:
    """Classify the observed studio reply text without exposing that text."""

    normalized = str(text or "").strip()
    if not normalized:
        return ()
    tags: List[str] = []
    rules = (
        ("price_quote", ("[金额]", "报价", "价格", "到手价", "元", "块")),
        ("promotion_offer", ("活动", "优惠", "折扣", "满减", "赠送", "送您")),
        ("product_recommendation", ("推荐", "适合", "建议", "尺码", "尺寸", "款式")),
        ("trust_proof", ("正品", "品质", "证书", "保障", "放心", "实拍")),
        ("scarcity_or_urgency", ("限时", "最后", "截止", "仅剩", "库存不多", "抓紧")),
        ("question_or_clarification", ("请问", "麻烦", "需要", "想要", "哪一款", "什么尺码")),
    )
    for tag, markers in rules:
        if _contains(normalized, markers):
            tags.append(tag)
    return tuple(tags or ["other_observed_reply"])


def _primary_talk_track(tags: Sequence[str]) -> str:
    priority = (
        "price_quote",
        "promotion_offer",
        "product_recommendation",
        "trust_proof",
        "scarcity_or_urgency",
        "question_or_clarification",
        "other_observed_reply",
    )
    return next((tag for tag in priority if tag in tags), "no_observed_reply")


def attribute_purchase_event(
    event: PurchaseEvent,
    episodes: Sequence[ContactEpisode],
    *,
    customer_key: Optional[str],
    identity_verified: bool,
    identity_shared: bool = False,
    lookback_days: int = 7,
) -> AttributionDecision:
    """Assign exactly one audit conclusion to one customer-day purchase event."""

    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    if not identity_verified or identity_shared or not customer_key:
        reasons = ("shared_approved_phone",) if identity_shared else ("no_unique_approved_identity",)
        return AttributionDecision(
            event.purchase_event_id,
            "identity_unverified",
            None,
            (),
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            reasons,
        )

    if set(event.quality_flags).intersection(_QUALITY_BLOCKING_FLAGS):
        return AttributionDecision(
            event.purchase_event_id,
            "quality_unknown",
            customer_key,
            (),
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            ("order_quality_blocking",),
        )

    candidates = [
        item
        for item in episodes
        if item.customer_key == customer_key
        and item.ended_at.date() <= event.paid_on
        and event.paid_on - timedelta(days=lookback_days) <= item.ended_at.date()
    ]
    candidates.sort(key=lambda item: (item.ended_at, item.episode_id))
    if not candidates:
        return AttributionDecision(
            event.purchase_event_id,
            "no_matching_contact",
            customer_key,
            (),
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            ("no_contact_episode_in_lookback",),
        )
    if len(candidates) > 1:
        return AttributionDecision(
            event.purchase_event_id,
            "competing_contact_episodes",
            customer_key,
            tuple(item.episode_id for item in candidates),
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            ("multiple_contact_episodes_in_lookback",),
        )

    selected = candidates[0]
    delta = (event.paid_on - selected.ended_at.date()).days
    same_day = delta == 0
    return AttributionDecision(
        event.purchase_event_id,
        "same_day_correlation" if same_day else "high_confidence_cross_day",
        customer_key,
        (selected.episode_id,),
        selected.origin,
        selected.intent,
        selected.explicit_price_barrier,
        selected.suspected_barrier,
        selected.talk_track_primary,
        delta,
        not same_day and selected.intent != "aftersales_or_order_status",
        (
            ("date_only_order_time", "same_day_sequence_unknown")
            if same_day
            else ("unique_cross_day_contact_episode",)
        ),
    )


def _table_columns(connection, table: str) -> set:
    return {str(row["name"]) for row in connection.execute("PRAGMA table_info(%s)" % table)}


def _load_episodes(connection) -> List[ContactEpisode]:
    rows = connection.execute(
        """
        SELECT dc.customer_key,dc.episode_id,dc.card_id,dc.card_type,dc.as_of_at,
               dc.action_window_end,dc.observation_until,dc.blind_context_json,
               dc.observed_action_json,aa.reply_strategy
        FROM decision_cards dc
        LEFT JOIN action_annotations aa ON aa.card_id=dc.card_id
        ORDER BY dc.customer_key,dc.episode_id,dc.as_of_at,dc.boundary_ordinal,dc.card_id
        """
    ).fetchall()
    grouped: Dict[Tuple[str, str], List[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["customer_key"]), str(row["episode_id"]))].append(row)

    output: List[ContactEpisode] = []
    for (customer_key, episode_id), cards in grouped.items():
        moments = [_moment(str(card["as_of_at"])) for card in cards]
        customer_texts: List[str] = []
        all_tags: List[str] = []
        latest_inbound: Optional[Mapping[str, object]] = None
        for card in cards:
            context = _json(card["blind_context_json"], [])
            if isinstance(context, list):
                customer_texts.extend(
                    str(item.get("text") or "")
                    for item in context
                    if isinstance(item, Mapping) and item.get("role") == "customer"
                )
            action = _json(card["observed_action_json"], {})
            if isinstance(action, Mapping):
                delay = action.get("reply_delay_seconds")
                if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay >= 0:
                    moments.append(_moment(str(card["as_of_at"])) + timedelta(seconds=float(delay)))
                for tag in classify_actual_talk_track(str(action.get("text") or "")):
                    if tag not in all_tags:
                        all_tags.append(tag)
            if card["card_type"] == "inbound":
                latest_inbound = card

        signal = classify_customer_signal("\n".join(customer_texts))
        suspected = "none"
        if latest_inbound is not None:
            action = _json(latest_inbound["observed_action_json"], {})
            tags = (
                classify_actual_talk_track(str(action.get("text") or ""))
                if isinstance(action, Mapping)
                else ()
            )
            observation_until = latest_inbound["observation_until"]
            window_end = latest_inbound["action_window_end"]
            complete = bool(
                observation_until
                and window_end
                and _moment(str(observation_until)) >= _moment(str(window_end))
            )
            if complete and "price_quote" in tags:
                suspected = "quote_then_silence_suspected"

        output.append(
            ContactEpisode(
                customer_key=customer_key,
                episode_id=episode_id,
                origin=(
                    "studio_initiated"
                    if any(card["card_type"] == "proactive_followup" for card in cards)
                    else "customer_initiated"
                ),
                started_at=min(moments),
                ended_at=max(moments),
                intent=signal["intent"],
                explicit_price_barrier=signal["explicit_price_barrier"],
                suspected_barrier=suspected,
                talk_track_tags=tuple(all_tags),
                talk_track_primary=_primary_talk_track(all_tags),
                card_count=len(cards),
            )
        )
    return sorted(output, key=lambda item: (item.started_at, item.customer_key, item.episode_id))


def _load_identity(connection) -> Tuple[Dict[str, str], set]:
    rows = connection.execute(
        """
        SELECT cl.customer_key,cl.phone_hmac,cl.state,coe.eligibility
        FROM conversation_links cl
        LEFT JOIN conversation_order_eligibility coe ON coe.customer_key=cl.customer_key
        """
    ).fetchall()
    phones_by_customer: Dict[str, set] = defaultdict(set)
    conflicted = set()
    for row in rows:
        customer = str(row["customer_key"])
        if row["state"] == "conflict":
            conflicted.add(customer)
        if (
            row["state"] == "approved"
            and row["eligibility"] in {"order_customer", "album_customer"}
            and row["phone_hmac"]
        ):
            phones_by_customer[customer].add(str(row["phone_hmac"]))

    customer_by_phone: Dict[str, str] = {}
    shared_phones = set()
    for customer, phones in phones_by_customer.items():
        if len(phones) != 1 or customer in conflicted:
            continue
        phone = next(iter(phones))
        if phone in customer_by_phone and customer_by_phone[phone] != customer:
            shared_phones.add(phone)
        else:
            customer_by_phone[phone] = customer
    for phone in shared_phones:
        customer_by_phone.pop(phone, None)
    return customer_by_phone, shared_phones


def _merge_identity_sources(
    current: Mapping[str, str],
    history: Mapping[str, str],
    current_shared: set,
    history_shared: set,
) -> Tuple[Dict[str, str], set]:
    customers_by_phone: Dict[str, set] = defaultdict(set)
    for source in (history, current):
        for phone, customer in source.items():
            customers_by_phone[phone].add(customer)
    shared = set(current_shared).union(history_shared)
    shared.update(phone for phone, customers in customers_by_phone.items() if len(customers) > 1)
    merged = {
        phone: next(iter(customers))
        for phone, customers in customers_by_phone.items()
        if len(customers) == 1 and phone not in shared
    }
    return merged, shared


def _event_id(phone_hmac: str, paid_on: date) -> str:
    digest = hashlib.sha256((phone_hmac + "\x1f" + paid_on.isoformat()).encode("utf-8")).hexdigest()
    return "purchase-event_" + digest[:24]


def _repeat_state(
    current: date, later: Sequence[date], days: int, observed_until: datetime
) -> Optional[bool]:
    if any(current < item <= current + timedelta(days=days) for item in later):
        return True
    window_end = datetime.combine(current + timedelta(days=days + 1), time.min, SHANGHAI)
    return False if observed_until >= window_end else None


def _load_purchase_events(
    connection, *, as_of: datetime
) -> Tuple[List[PurchaseEvent], Dict[str, object]]:
    active = connection.execute(
        "SELECT order_snapshot_id,synced_at,record_count,quality_json "
        "FROM order_snapshots WHERE state='active'"
    ).fetchone()
    if active is None:
        raise RuntimeError("conversion audit requires one active order snapshot")
    observed_until = _moment(str(active["synced_at"]))
    normalized_quality = _json(active["quality_json"], {})
    report_quality = dict(normalized_quality) if isinstance(normalized_quality, Mapping) else {}
    if "phone_hmac_records" in report_quality:
        report_quality["identity_key_records"] = report_quality.pop("phone_hmac_records")
    columns = _table_columns(connection, "orders")
    refund_expression = "refund_amount_minor" if "refund_amount_minor" in columns else "NULL"
    rows = connection.execute(
        "SELECT phone_hmac,paid_on,revenue_minor,%s AS refund_amount_minor,quality_flags_json "
        "FROM orders WHERE order_snapshot_id=?" % refund_expression,
        (active["order_snapshot_id"],),
    ).fetchall()

    grouped: Dict[Tuple[str, date], Dict[str, object]] = {}
    quality_counts = (
        normalized_quality.get("quality_flag_counts", {})
        if isinstance(normalized_quality, Mapping)
        else {}
    )
    future_records = int(quality_counts.get("future_paid_on", 0) or 0)
    invalid_or_unpaid_records = 0
    for row in rows:
        phone = str(row["phone_hmac"] or "")
        paid_text = str(row["paid_on"] or "")[:10]
        try:
            paid_on = date.fromisoformat(paid_text)
        except ValueError:
            invalid_or_unpaid_records += 1
            continue
        revenue = row["revenue_minor"]
        if not phone or revenue is None or int(revenue) <= 0:
            invalid_or_unpaid_records += 1
            continue
        if paid_on > as_of.date():
            future_records += 1
            continue
        key = (phone, paid_on)
        item = grouped.setdefault(
            key,
            {"records": 0, "gross": 0, "refund": 0, "flags": set()},
        )
        item["records"] = int(item["records"]) + 1
        item["gross"] = int(item["gross"]) + int(revenue)
        refund = row["refund_amount_minor"]
        item["refund"] = int(item["refund"]) + max(0, int(refund or 0))
        flags = _json(row["quality_flags_json"], [])
        if isinstance(flags, list):
            item["flags"].update(str(flag) for flag in flags)

    dates_by_phone: Dict[str, List[date]] = defaultdict(list)
    for phone, paid_on in grouped:
        dates_by_phone[phone].append(paid_on)
    for values in dates_by_phone.values():
        values.sort()

    events: List[PurchaseEvent] = []
    for (phone, paid_on), item in grouped.items():
        dates = dates_by_phone[phone]
        prior = [value for value in dates if value < paid_on]
        later = [value for value in dates if value > paid_on]
        gross = int(item["gross"])
        refund = int(item["refund"])
        events.append(
            PurchaseEvent(
                purchase_event_id=_event_id(phone, paid_on),
                phone_hmac=phone,
                paid_on=paid_on,
                source_record_count=int(item["records"]),
                gross_revenue_minor=gross,
                net_revenue_minor=max(0, gross - refund),
                quality_flags=tuple(sorted(item["flags"])),
                prior_purchase_count=len(prior),
                repeat_30d=_repeat_state(paid_on, later, 30, observed_until),
                repeat_60d=_repeat_state(paid_on, later, 60, observed_until),
                repeat_90d=_repeat_state(paid_on, later, 90, observed_until),
            )
        )
    events.sort(key=lambda item: (item.paid_on, item.purchase_event_id))
    return events, {
        "order_snapshot_id": str(active["order_snapshot_id"]),
        "order_synced_at": observed_until.isoformat(timespec="seconds"),
        "active_order_record_count": int(active["record_count"]),
        "normalized_order_quality": report_quality,
        "future_paid_records_excluded": future_records,
        "invalid_unpaid_or_unlinked_records_excluded": invalid_or_unpaid_records,
    }


def _merge_purchase_event_sources(
    current: Sequence[PurchaseEvent],
    history: Sequence[PurchaseEvent],
    *,
    observed_until: datetime,
) -> Tuple[List[PurchaseEvent], Dict[str, int]]:
    """Merge overlapping snapshots at customer-day event grain, then recompute repeats."""

    merged = {item.purchase_event_id: item for item in history}
    overlap = 0
    for item in current:
        previous = merged.get(item.purchase_event_id)
        if previous is None:
            merged[item.purchase_event_id] = item
            continue
        overlap += 1
        gross = max(previous.gross_revenue_minor, item.gross_revenue_minor)
        refund = max(
            previous.gross_revenue_minor - previous.net_revenue_minor,
            item.gross_revenue_minor - item.net_revenue_minor,
        )
        merged[item.purchase_event_id] = replace(
            item,
            source_record_count=max(previous.source_record_count, item.source_record_count),
            gross_revenue_minor=gross,
            net_revenue_minor=max(0, gross - max(0, refund)),
            quality_flags=tuple(
                sorted(set(previous.quality_flags).union(item.quality_flags, {"cross_snapshot_overlap"}))
            ),
        )

    dates_by_phone: Dict[str, List[date]] = defaultdict(list)
    for item in merged.values():
        dates_by_phone[item.phone_hmac].append(item.paid_on)
    for values in dates_by_phone.values():
        values.sort()

    output: List[PurchaseEvent] = []
    for item in merged.values():
        dates = dates_by_phone[item.phone_hmac]
        prior = [value for value in dates if value < item.paid_on]
        later = [value for value in dates if value > item.paid_on]
        output.append(
            replace(
                item,
                prior_purchase_count=len(prior),
                repeat_30d=_repeat_state(item.paid_on, later, 30, observed_until),
                repeat_60d=_repeat_state(item.paid_on, later, 60, observed_until),
                repeat_90d=_repeat_state(item.paid_on, later, 90, observed_until),
            )
        )
    output.sort(key=lambda item: (item.paid_on, item.purchase_event_id))
    return output, {
        "history_purchase_event_count": len(history),
        "current_purchase_event_count": len(current),
        "cross_snapshot_overlap_event_count": overlap,
        "merged_purchase_event_count": len(output),
    }


def _message_source_metadata(connection) -> Dict[str, object]:
    row = connection.execute(
        """
        SELECT observed_until,last_at,record_count,snapshot_id
        FROM source_snapshots
        WHERE source_kind='live-inbox-events'
        ORDER BY captured_at DESC,snapshot_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {
            "message_snapshot_id": None,
            "message_observed_until": None,
            "latest_message_at": None,
            "message_source_record_count": None,
        }
    return {
        "message_snapshot_id": str(row["snapshot_id"]),
        "message_observed_until": row["observed_until"],
        "latest_message_at": row["last_at"],
        "message_source_record_count": row["record_count"],
    }


def _episode_samples(
    episodes: Sequence[ContactEpisode],
    decisions: Sequence[AttributionDecision],
    events_by_id: Mapping[str, PurchaseEvent],
    customer_by_phone: Mapping[str, str],
    order_observed_until: datetime,
) -> List[Dict[str, object]]:
    events_by_episode: Dict[str, List[AttributionDecision]] = defaultdict(list)
    ambiguous_episodes = set()
    for decision in decisions:
        if decision.attribution_state == "competing_contact_episodes":
            ambiguous_episodes.update(decision.episode_ids)
        elif decision.episode_ids:
            events_by_episode[decision.episode_ids[0]].append(decision)

    verified_customers = set(customer_by_phone.values())
    samples: List[Dict[str, object]] = []
    for episode in episodes:
        decisions_for_episode = events_by_episode.get(episode.episode_id, [])
        clean_positive = [
            item
            for item in decisions_for_episode
            if item.attribution_state == "high_confidence_cross_day"
        ]
        same_day = [
            item for item in decisions_for_episode if item.attribution_state == "same_day_correlation"
        ]
        if episode.customer_key not in verified_customers:
            state = "identity_unverified"
        elif episode.episode_id in ambiguous_episodes or same_day:
            state = "ambiguous"
        elif clean_positive:
            state = "converted_7d"
        else:
            final_midnight = datetime.combine(
                episode.ended_at.date() + timedelta(days=8), time.min, SHANGHAI
            )
            state = "non_converted_7d" if order_observed_until >= final_midnight else "censored"

        purchase_ids = tuple(item.purchase_event_id for item in clean_positive)
        repeat_90 = [events_by_id[item].repeat_90d for item in purchase_ids]
        samples.append(
            {
                "episode_id": episode.episode_id,
                "customer_key": episode.customer_key,
                "origin": episode.origin,
                "intent": episode.intent,
                "explicit_price_barrier": episode.explicit_price_barrier,
                "suspected_barrier": episode.suspected_barrier,
                "talk_track_tags": list(episode.talk_track_tags),
                "talk_track_primary": episode.talk_track_primary,
                "ended_on": episode.ended_at.date().isoformat(),
                "sample_state": state,
                "purchase_event_ids": list(purchase_ids),
                "repeat_90d": (
                    True
                    if any(value is True for value in repeat_90)
                    else False
                    if repeat_90 and all(value is False for value in repeat_90)
                    else None
                ),
                "eligible_for_sales_method": (
                    state in {"converted_7d", "non_converted_7d"}
                    and episode.intent != "aftersales_or_order_status"
                ),
            }
        )
    return samples


def _cohort_rows(
    samples: Sequence[Mapping[str, object]], minimum_customers: int
) -> Dict[str, List[Dict[str, object]]]:
    dimensions = (
        "origin",
        "intent",
        "explicit_price_barrier",
        "suspected_barrier",
        "talk_track_primary",
    )
    output: Dict[str, List[Dict[str, object]]] = {}
    for dimension in dimensions:
        groups: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
        for sample in samples:
            groups[str(sample[dimension])].append(sample)
        rows: List[Dict[str, object]] = []
        for label, values in groups.items():
            by_customer: Dict[str, List[str]] = defaultdict(list)
            for item in values:
                if item.get("eligible_for_sales_method"):
                    by_customer[str(item["customer_key"])].append(str(item["sample_state"]))
            all_customers = {str(item["customer_key"]) for item in values}
            positive = 0
            negative = 0
            for states in by_customer.values():
                if "converted_7d" in states:
                    positive += 1
                elif "non_converted_7d" in states:
                    negative += 1
            clean = positive + negative
            visible = clean >= minimum_customers
            rows.append(
                {
                    "label": label,
                    "episode_count": len(values),
                    "independent_customer_count": len(all_customers),
                    "clean_customer_count": clean,
                    "converted_customer_count": positive,
                    "non_converted_customer_count": negative,
                    "association_rate_7d": round(positive / clean, 6) if visible else None,
                    "statistics_visible": visible,
                    "minimum_independent_customers": minimum_customers,
                }
            )
        output[dimension] = sorted(
            rows, key=lambda item: (-int(item["episode_count"]), str(item["label"]))
        )
    return output


def _attribution_row(
    event: PurchaseEvent, decision: AttributionDecision
) -> Dict[str, object]:
    row = asdict(decision)
    row["episode_ids"] = list(decision.episode_ids)
    row["reason_codes"] = list(decision.reason_codes)
    row.update(
        {
            "paid_on": event.paid_on.isoformat(),
            "source_record_count": event.source_record_count,
            "gross_revenue_minor": event.gross_revenue_minor,
            "net_revenue_minor": event.net_revenue_minor,
            "prior_purchase_count": event.prior_purchase_count,
            "is_repeat_purchase": event.prior_purchase_count > 0,
            "repeat_30d": event.repeat_30d,
            "repeat_60d": event.repeat_60d,
            "repeat_90d": event.repeat_90d,
            "quality_flags": list(event.quality_flags),
        }
    )
    return row


def _markdown_report(report: Mapping[str, object]) -> str:
    attribution = report["attribution_counts"]
    samples = report["episode_sample_counts"]
    freshness = report["source_freshness"]
    gate = report["training_gate"]
    lines = [
        "# 成交归因样本审计 V1",
        "",
        "本报告只做历史相关性审计，不证明某次触达或某句话术导致成交；本次未训练权重，也未产生可发送消息。",
        "",
        "## 数据截止与完整性",
        "",
        "- 请求截止：`%s`" % report["requested_as_of"],
        "- 聊天快照观察到：`%s`" % freshness.get("message_observed_until"),
        "- 订单快照同步到：`%s`" % freshness.get("order_synced_at"),
        "- 是否覆盖请求截止：`%s`" % freshness.get("covers_requested_as_of"),
        "",
        "## 一笔购买事件一个归因结论",
        "",
    ]
    labels = {
        "high_confidence_cross_day": "高置信：唯一跨日接触回合",
        "same_day_correlation": "同日相关：订单仅有日期，先后未知",
        "competing_contact_episodes": "多次接触竞争归因",
        "no_matching_contact": "无匹配接触回合",
        "identity_unverified": "身份未唯一核验",
        "quality_unknown": "订单质量字段不足",
    }
    for key in ATTRIBUTION_STATES:
        lines.append("- %s：%s" % (labels[key], attribution.get(key, 0)))
    lines.extend(
        [
            "",
            "## 跟进回合样本",
            "",
            "- 可用成交正样本：%s" % samples.get("converted_7d", 0),
            "- 可比未成交样本：%s" % samples.get("non_converted_7d", 0),
            "- 歧义样本：%s" % samples.get("ambiguous", 0),
            "- 观察期未满：%s" % samples.get("censored", 0),
            "- 身份未核验：%s" % samples.get("identity_unverified", 0),
            "",
            "## 当前训练门",
            "",
            "- 是否允许训练：`%s`" % gate.get("ready"),
            "- 本次是否训练权重：`%s`" % gate.get("weights_trained"),
        ]
    )
    for blocker in gate.get("blockers", []):
        lines.append("- 阻碍：`%s`" % blocker)
    lines.extend(
        [
            "",
            "下一步应先补齐同一截止点的聊天与订单派生快照，再对高置信正样本和可比未成交样本做人工抽检；通过后才进入权重学习和影子验证。",
            "",
        ]
    )
    return "\n".join(lines)


def build_conversion_audit(
    db_path: Path,
    *,
    as_of_at: str,
    output_dir: Path,
    facts_db_path: Optional[Path] = None,
    history_facts_db_path: Optional[Path] = None,
    lookback_days: int = 7,
    minimum_customers: int = 30,
    project_root: Optional[Path] = None,
) -> Dict[str, object]:
    """Build aggregate and opaque row-level derived artifacts from a DB opened read-only."""

    if minimum_customers < 1:
        raise ValueError("minimum_customers must be positive")
    as_of = _moment(as_of_at)
    root = Path(project_root or Path.cwd()).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    assert_project_output(destination / "report.json", root)

    message_db = Path(db_path).expanduser().resolve()
    facts_db = Path(facts_db_path or db_path).expanduser().resolve()
    connection = open_store(str(message_db), read_only=True)
    try:
        message_meta = _message_source_metadata(connection)
        episodes = _load_episodes(connection)
    finally:
        connection.close()
    facts_connection = open_store(str(facts_db), read_only=True)
    try:
        customer_by_phone, shared_phones = _load_identity(facts_connection)
        current_events, order_meta = _load_purchase_events(facts_connection, as_of=as_of)
    finally:
        facts_connection.close()
    order_observed_until = _moment(str(order_meta["order_synced_at"]))
    merge_meta: Dict[str, int] = {
        "history_purchase_event_count": 0,
        "current_purchase_event_count": len(current_events),
        "cross_snapshot_overlap_event_count": 0,
        "merged_purchase_event_count": len(current_events),
    }
    events = current_events
    if history_facts_db_path is not None:
        history_connection = open_store(
            str(Path(history_facts_db_path).expanduser().resolve()), read_only=True
        )
        try:
            history_events, history_meta = _load_purchase_events(history_connection, as_of=as_of)
            history_identity, history_shared = _load_identity(history_connection)
        finally:
            history_connection.close()
        events, merge_meta = _merge_purchase_event_sources(
            current_events,
            history_events,
            observed_until=order_observed_until,
        )
        customer_by_phone, shared_phones = _merge_identity_sources(
            customer_by_phone,
            history_identity,
            shared_phones,
            history_shared,
        )
        order_meta["history_order_snapshot_id"] = history_meta["order_snapshot_id"]
        order_meta["history_order_synced_at"] = history_meta["order_synced_at"]
        order_meta["purchase_event_merge"] = merge_meta

    episodes_by_customer: Dict[str, List[ContactEpisode]] = defaultdict(list)
    for episode in episodes:
        episodes_by_customer[episode.customer_key].append(episode)
    decisions: List[AttributionDecision] = []
    for event in events:
        customer = customer_by_phone.get(event.phone_hmac)
        decisions.append(
            attribute_purchase_event(
                event,
                episodes_by_customer.get(customer or "", ()),
                customer_key=customer,
                identity_verified=customer is not None,
                identity_shared=event.phone_hmac in shared_phones,
                lookback_days=lookback_days,
            )
        )

    attribution_rows = [
        _attribution_row(event, decision) for event, decision in zip(events, decisions)
    ]
    events_by_id = {item.purchase_event_id: item for item in events}
    samples = _episode_samples(
        episodes,
        decisions,
        events_by_id,
        customer_by_phone,
        order_observed_until,
    )

    message_observed = message_meta.get("message_observed_until")
    message_covers = bool(message_observed and _moment(str(message_observed)) >= as_of)
    order_covers = order_observed_until >= as_of
    blockers = []
    if not message_covers:
        blockers.append("message_snapshot_does_not_cover_requested_as_of")
    if not order_covers:
        blockers.append("order_snapshot_does_not_cover_requested_as_of")
    blockers.append("manual_sample_review_not_completed")

    attribution_counts = Counter(item.attribution_state for item in decisions)
    sample_counts = Counter(str(item["sample_state"]) for item in samples)
    high_confidence_customers = {
        item.customer_key
        for item in decisions
        if item.eligible_for_method_learning and item.customer_key
    }
    report: Dict[str, object] = {
        "audit_version": AUDIT_VERSION,
        "claim_mode": "historical_association_only_no_causal_claim",
        "source_databases_opened_read_only": True,
        "split_message_and_fact_snapshots": message_db != facts_db,
        "history_facts_merged": history_facts_db_path is not None,
        "weights_trained": False,
        "requested_as_of": as_of.isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "source_freshness": {
            **message_meta,
            **order_meta,
            "message_covers_requested_as_of": message_covers,
            "order_covers_requested_as_of": order_covers,
            "covers_requested_as_of": message_covers and order_covers,
        },
        "population": {
            "contact_episode_count": len(episodes),
            "purchase_event_count": len(events),
            "approved_unique_linked_phone_count": len(customer_by_phone),
            "shared_approved_phone_count": len(shared_phones),
            "repeat_purchase_event_count": sum(item.prior_purchase_count > 0 for item in events),
            **merge_meta,
        },
        "attribution_counts": {
            key: int(attribution_counts.get(key, 0)) for key in ATTRIBUTION_STATES
        },
        "episode_sample_counts": dict(sorted(sample_counts.items())),
        "cohorts": _cohort_rows(samples, minimum_customers),
        "training_gate": {
            "ready": False,
            "weights_trained": False,
            "minimum_independent_customers": minimum_customers,
            "high_confidence_independent_customer_count": len(high_confidence_customers),
            "manual_review_required": True,
            "blockers": blockers,
        },
    }

    destination.mkdir(parents=True, exist_ok=True)
    report_json = destination / "report.json"
    report_markdown = destination / "report.zh.md"
    attribution_jsonl = destination / "purchase_attribution.jsonl"
    samples_jsonl = destination / "episode_samples.jsonl"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_markdown.write_text(_markdown_report(report), encoding="utf-8")
    attribution_jsonl.write_text(
        "".join(json_dumps(row) + "\n" for row in attribution_rows), encoding="utf-8"
    )
    samples_jsonl.write_text(
        "".join(json_dumps(row) + "\n" for row in samples), encoding="utf-8"
    )
    return {
        "audit_version": AUDIT_VERSION,
        "output_dir": str(destination),
        "report_json": str(report_json),
        "report_markdown": str(report_markdown),
        "purchase_attribution_jsonl": str(attribution_jsonl),
        "episode_samples_jsonl": str(samples_jsonl),
        "purchase_event_count": len(events),
        "contact_episode_count": len(episodes),
        "training_ready": False,
        "weights_trained": False,
    }
