"""Attach post-decision order facts without changing blind decision cards."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from .cards import DecisionCard
from .core import parse_timestamp
from .orders import CanonicalOrder


SHANGHAI = ZoneInfo("Asia/Shanghai")
ORDER_ELIGIBLE = frozenset({"order_customer", "album_customer"})
RETURN_TYPES = frozenset({"return", "return_taro"})
AFTERSALE_TYPES = frozenset(
    {"cancel", "return", "return_taro", "exchange", "compensation", "other"}
)
COMPLETED_STATES = frozenset(
    {"close", "closed", "complete", "completed", "done", "完成", "已完成", "结束", "已结束"}
)
QUALITY_FLAGS = frozenset(
    {
        "missing_refund_on",
        "missing_refund_amount",
        "invalid_refund_on",
        "invalid_refund_amount",
        "refund_exceeds_revenue",
        "future_refund_on",
        "aftersale_open",
    }
)
ATTRIBUTION_FLAG_ORDER = (
    "same_day",
    "multiple_cards",
    "multiple_orders",
    "shared_phone_multiple_conversations",
)


@dataclass(frozen=True)
class ConversationLink:
    """The reviewed identity and independent order-eligibility decision."""

    customer_key: str
    phone_hmac: Optional[str]
    state: str
    eligibility: str


@dataclass(frozen=True)
class CardOutcome:
    card_id: str
    paid_1d: Optional[bool]
    paid_3d: Optional[bool]
    paid_7d: Optional[bool]
    retained_30d: Optional[bool]
    aftersale_30d: Optional[bool]
    exchange_30d: Optional[bool]
    compensation_30d: Optional[bool]
    refund_loss_ratio: Optional[float]
    attribution_state: str
    attribution_flags: Tuple[str, ...]
    matched_orders: Tuple[str, ...]
    computed_at: str


def _card_moment(card: DecisionCard) -> datetime:
    value = parse_timestamp(card.as_of_at)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _observed_moment(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("orders_observed_until must include a timezone")
    return value.astimezone(SHANGHAI)


def _order_date(order: CanonicalOrder) -> Optional[date]:
    if not order.paid_on or order.revenue_minor is None or order.revenue_minor <= 0:
        return None
    try:
        return date.fromisoformat(order.paid_on[:10])
    except ValueError:
        return None


def _refund_date(order: CanonicalOrder) -> Optional[date]:
    if not order.refund_on:
        return None
    try:
        return date.fromisoformat(order.refund_on[:10])
    except ValueError:
        return None


def _observed_through_day(observed_until: datetime, final_day: date) -> bool:
    next_midnight = datetime.combine(final_day + timedelta(days=1), time.min, SHANGHAI)
    return observed_until >= next_midnight


def _paid_state(
    card_day: date,
    days: int,
    matched_orders: Sequence[CanonicalOrder],
    observed_until: datetime,
) -> Optional[bool]:
    final_day = card_day + timedelta(days=days)
    if any(
        order_day is not None and card_day <= order_day <= final_day
        for order_day in (_order_date(item) for item in matched_orders)
    ):
        return True
    if _observed_through_day(observed_until, final_day):
        return False
    return None


def _is_event_within_30_days(order: CanonicalOrder) -> bool:
    paid_on = _order_date(order)
    if paid_on is None or order.refund_type not in AFTERSALE_TYPES:
        return False
    event_on = _refund_date(order)
    if event_on is None:
        # Exchange and compensation sources do not require an event date.
        # Cancel is itself a terminal order fact.  Returns are handled as
        # quality-unknown when their required date is absent.
        return order.refund_type in {"cancel", "exchange", "compensation", "other"}
    return paid_on <= event_on <= paid_on + timedelta(days=30)


def _return_status_complete(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in COMPLETED_STATES


def _order_quality_unknown(order: CanonicalOrder) -> bool:
    flags = set(order.quality_flags)
    if flags.intersection(QUALITY_FLAGS):
        return True
    if order.refund_type in RETURN_TYPES:
        if order.refund_on is None or order.refund_amount_minor is None:
            return True
    paid_on = _order_date(order)
    refund_on = _refund_date(order)
    if paid_on is not None and refund_on is not None and refund_on < paid_on:
        return True
    if (
        order.refund_amount_minor is not None
        and order.revenue_minor is not None
        and (order.refund_amount_minor < 0 or order.refund_amount_minor > order.revenue_minor)
    ):
        return True
    if order.refund_type == "exchange":
        if order.refund_amount_minor not in (None, 0) or not _return_status_complete(order.return_status):
            return True
    if order.refund_type == "other":
        return True
    return False


def _all_thirty_day_windows_observed(
    orders: Sequence[CanonicalOrder], observed_until: datetime
) -> bool:
    paid_dates = [_order_date(item) for item in orders]
    return bool(paid_dates) and all(
        paid_on is not None
        and _observed_through_day(observed_until, paid_on + timedelta(days=30))
        for paid_on in paid_dates
    )


def _event_state(
    orders: Sequence[CanonicalOrder],
    observed_until: datetime,
    event_types: Set[str],
) -> Optional[bool]:
    if any(
        item.refund_type in event_types and _is_event_within_30_days(item)
        for item in orders
    ):
        return True
    if any(
        item.refund_type in event_types
        and item.refund_type in RETURN_TYPES
        and _refund_date(item) is None
        for item in orders
    ):
        return None
    if _all_thirty_day_windows_observed(orders, observed_until):
        return False
    return None


def _refund_loss_ratio(orders: Sequence[CanonicalOrder]) -> Optional[float]:
    if not orders or any(_order_quality_unknown(item) for item in orders):
        return None
    revenue = sum(int(item.revenue_minor or 0) for item in orders)
    if revenue <= 0:
        return None
    refunded = 0
    for item in orders:
        if item.refund_type == "cancel" and item.refund_amount_minor is None:
            return None
        if item.refund_type in RETURN_TYPES.union({"cancel"}):
            if item.refund_amount_minor is None:
                return None
            if _is_event_within_30_days(item):
                refunded += item.refund_amount_minor
        elif item.refund_amount_minor:
            refunded += item.refund_amount_minor
    return refunded / revenue


def _retained_state(
    orders: Sequence[CanonicalOrder], observed_until: datetime
) -> Optional[bool]:
    if not orders or any(_order_quality_unknown(item) for item in orders):
        return None
    total_revenue = sum(int(item.revenue_minor or 0) for item in orders)
    if total_revenue <= 0:
        return None
    lost = 0
    for item in orders:
        if item.refund_type == "cancel" and _is_event_within_30_days(item):
            lost += int(item.revenue_minor or 0)
        elif item.refund_type in RETURN_TYPES and _is_event_within_30_days(item):
            if item.refund_amount_minor is None:
                return None
            lost += item.refund_amount_minor
    if lost >= total_revenue:
        return False
    if not _all_thirty_day_windows_observed(orders, observed_until):
        return None
    return True


def _eligible_phone_by_customer(
    links: Sequence[ConversationLink],
) -> Dict[str, Optional[str]]:
    customers = {item.customer_key for item in links}
    valid: Dict[str, Set[str]] = defaultdict(set)
    for item in links:
        if (
            item.state == "approved"
            and item.eligibility in ORDER_ELIGIBLE
            and item.phone_hmac
        ):
            valid[item.customer_key].add(item.phone_hmac)
    return {
        customer: next(iter(phones)) if len(phones) == 1 else None
        for customer, phones in ((customer, valid.get(customer, set())) for customer in customers)
    }


def _unverified(card_id: str, computed_at: str) -> CardOutcome:
    return CardOutcome(
        card_id=card_id,
        paid_1d=None,
        paid_3d=None,
        paid_7d=None,
        retained_30d=None,
        aftersale_30d=None,
        exchange_30d=None,
        compensation_30d=None,
        refund_loss_ratio=None,
        attribution_state="identity_unverified",
        attribution_flags=(),
        matched_orders=(),
        computed_at=computed_at,
    )


def attach_outcomes(
    cards: Sequence[DecisionCard],
    conversation_links: Sequence[ConversationLink],
    orders: Sequence[CanonicalOrder],
    *,
    orders_observed_until: datetime,
    computed_at: Optional[datetime] = None,
) -> Dict[str, CardOutcome]:
    """Batch-attach tri-state outcomes through reviewed phone HMAC links.

    Batch processing is required so one date-only order can mark every
    competing card as ambiguous instead of being counted repeatedly as an
    independently associated success.
    """

    observed_until = _observed_moment(orders_observed_until)
    computed = computed_at or datetime.now(tz=SHANGHAI)
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=SHANGHAI)
    computed_iso = computed.astimezone(SHANGHAI).isoformat(timespec="seconds")

    by_id: Dict[str, DecisionCard] = {}
    for item in cards:
        if item.card_id in by_id:
            raise ValueError("card IDs must be unique")
        by_id[item.card_id] = item

    phone_by_customer = _eligible_phone_by_customer(conversation_links)
    cards_by_phone: Dict[str, List[DecisionCard]] = defaultdict(list)
    for item in cards:
        phone_hmac = phone_by_customer.get(item.customer_key)
        if phone_hmac:
            cards_by_phone[phone_hmac].append(item)

    paid_orders_by_phone: Dict[str, List[CanonicalOrder]] = defaultdict(list)
    seen_orders: Dict[str, CanonicalOrder] = {}
    for item in orders:
        if not item.phone_hmac or _order_date(item) is None:
            continue
        previous = seen_orders.get(item.order_line_id)
        if previous is not None:
            if previous != item:
                raise ValueError("duplicate order line IDs contain conflicting facts")
            continue
        seen_orders[item.order_line_id] = item
        paid_orders_by_phone[item.phone_hmac].append(item)
    for phone_orders in paid_orders_by_phone.values():
        phone_orders.sort(key=lambda item: (_order_date(item), item.order_line_id))

    matched_by_card: Dict[str, List[CanonicalOrder]] = defaultdict(list)
    flags_by_card: Dict[str, Set[str]] = defaultdict(set)
    for phone_hmac, phone_cards in cards_by_phone.items():
        for item in paid_orders_by_phone.get(phone_hmac, []):
            paid_on = _order_date(item)
            assert paid_on is not None
            candidates = [
                candidate
                for candidate in phone_cards
                if _card_moment(candidate).date()
                <= paid_on
                <= _card_moment(candidate).date() + timedelta(days=7)
            ]
            if not candidates:
                continue
            for candidate in candidates:
                matched_by_card[candidate.card_id].append(item)
                if paid_on == _card_moment(candidate).date():
                    flags_by_card[candidate.card_id].add("same_day")
            if len(candidates) > 1:
                for candidate in candidates:
                    flags_by_card[candidate.card_id].add("multiple_cards")
            if len({candidate.customer_key for candidate in candidates}) > 1:
                for candidate in candidates:
                    flags_by_card[candidate.card_id].add(
                        "shared_phone_multiple_conversations"
                    )

    output: Dict[str, CardOutcome] = {}
    for item in cards:
        phone_hmac = phone_by_customer.get(item.customer_key)
        if not phone_hmac:
            output[item.card_id] = _unverified(item.card_id, computed_iso)
            continue

        matched = sorted(
            matched_by_card.get(item.card_id, []),
            key=lambda order: (_order_date(order), order.order_line_id),
        )
        flags = flags_by_card[item.card_id]
        if len(matched) > 1:
            flags.add("multiple_orders")
        ordered_flags = tuple(flag for flag in ATTRIBUTION_FLAG_ORDER if flag in flags)
        quality_unknown = any(_order_quality_unknown(order) for order in matched)
        if quality_unknown:
            attribution_state = "quality_unknown"
        elif ordered_flags:
            attribution_state = "ambiguous"
        elif matched:
            attribution_state = "associated"
        else:
            attribution_state = "none"

        card_day = _card_moment(item).date()
        output[item.card_id] = CardOutcome(
            card_id=item.card_id,
            paid_1d=_paid_state(card_day, 1, matched, observed_until),
            paid_3d=_paid_state(card_day, 3, matched, observed_until),
            paid_7d=_paid_state(card_day, 7, matched, observed_until),
            retained_30d=_retained_state(matched, observed_until),
            aftersale_30d=_event_state(
                matched, observed_until, set(AFTERSALE_TYPES)
            )
            if matched
            else None,
            exchange_30d=_event_state(matched, observed_until, {"exchange"})
            if matched
            else None,
            compensation_30d=_event_state(
                matched, observed_until, {"compensation"}
            )
            if matched
            else None,
            refund_loss_ratio=_refund_loss_ratio(matched),
            attribution_state=attribution_state,
            attribution_flags=ordered_flags,
            matched_orders=tuple(order.order_line_id for order in matched),
            computed_at=computed_iso,
        )
    return output
