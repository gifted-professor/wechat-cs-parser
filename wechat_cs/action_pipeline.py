"""Persist the privacy-safe Plan 4.5--7 action artifacts.

The pipeline reads only the normalized SQLite facts produced by M0.  It never
reads the live inbox or dashboard source files, changes an M0 acceptance gate,
calls a model, or sends a message.  Every model-facing decision context remains
separate from the action and outcome observed after its decision boundary.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .action_queue import (
    POLICY_VERSION,
    PROHIBITED_CLAIMS,
    ActionCandidate,
    QueueContext,
    build_action_queue,
)
from .cards import CardSource, DecisionCard, build_decision_cards
from .core import DEFAULT_HMAC_SECRET, Message, hmac_id, json_dumps
from .customer_features import (
    FEATURE_RULE_VERSION,
    MANUAL_CONTACT_WINDOW,
    CustomerFeatureSnapshot,
    build_customer_profiles,
    day_of_month_bucket,
)
from .orders import CanonicalOrder
from .outcomes import CardOutcome, ConversationLink, attach_outcomes
from .source_snapshot import hmac_key_fingerprint
from .store import initialize_schema, open_store


SHANGHAI = ZoneInfo("Asia/Shanghai")
PIPELINE_VERSION = "action-pipeline-v1"
ANNOTATION_RULE_VERSION = "reply-audit-rules-v1"
COMPLETED_RETURN_STATES = frozenset(
    {"close", "closed", "complete", "completed", "done", "完成", "已完成", "结束", "已结束"}
)

_POSITIVE_MARKERS = (
    "想要",
    "喜欢",
    "可以",
    "好的",
    "下单",
    "怎么买",
    "还有吗",
    "合适",
    "需要",
)
_NEGATIVE_MARKERS = (
    "不要了",
    "不需要",
    "别联系",
    "不用跟进",
    "暂时不",
    "算了",
    "不合适",
    "太贵",
)
_CONTACT_REJECTION_PATTERNS = (
    re.compile(
        r"(?:别|不要|不用|无需|不必|不需要|请勿).{0,6}"
        r"(?:再)?(?:联系|跟进|打扰|找我|发(?:消息|信息|微信))"
    ),
    re.compile(
        r"(?:以后|之后).{0,6}(?:别|不要|不用).{0,6}"
        r"(?:联系|跟进|打扰|找我|发(?:消息|信息|微信))"
    ),
    re.compile(r"(?:取消订阅|停止联系|停止跟进|退订|别再发消息|不要再发消息)"),
)
_AFTERSALES_MARKERS = (
    "售后",
    "退款",
    "退货",
    "换货",
    "破损",
    "少件",
    "补偿",
    "质量问题",
)
_GUARANTEE_MARKERS = ("保证", "肯定有货", "一定到", "马上退款", "绝对")


@dataclass(frozen=True)
class _ProfileSource:
    profile_id: str
    run_id: str
    account_state: str
    customer_keys: Tuple[str, ...]
    snapshot_by_customer: Mapping[str, str]
    observed_by_customer: Mapping[str, Optional[datetime]]
    profile_observed_until: Optional[datetime]
    source_healthy: bool


@dataclass(frozen=True)
class _OrderSource:
    order_snapshot_id: str
    run_id: str
    synced_at: datetime
    rows: Tuple[CanonicalOrder, ...]


@dataclass(frozen=True)
class _FeatureInputIndex:
    """Per-profile indexes used by historical point-in-time feature builds."""

    identity_by_customer: Mapping[str, Tuple[Mapping[str, object], ...]]
    identity_by_phone: Mapping[str, Tuple[Mapping[str, object], ...]]
    messages_by_customer: Mapping[str, Tuple[Message, ...]]
    orders_by_phone: Mapping[str, Tuple[CanonicalOrder, ...]]


def _index_feature_inputs(
    identity_rows: Sequence[Mapping[str, object]],
    orders: Sequence[CanonicalOrder],
    messages: Sequence[Message],
) -> _FeatureInputIndex:
    identity_by_customer: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
    identity_by_phone: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
    messages_by_customer: Dict[str, List[Message]] = defaultdict(list)
    orders_by_phone: Dict[str, List[CanonicalOrder]] = defaultdict(list)
    for row in identity_rows:
        customer_key = str(row.get("customer_key") or "")
        phone_hmac = str(row.get("phone_hmac") or "")
        if customer_key:
            identity_by_customer[customer_key].append(row)
        if phone_hmac:
            identity_by_phone[phone_hmac].append(row)
    for item in messages:
        messages_by_customer[item.customer_key].append(item)
    for item in orders:
        if item.phone_hmac:
            orders_by_phone[item.phone_hmac].append(item)
    return _FeatureInputIndex(
        identity_by_customer={key: tuple(value) for key, value in identity_by_customer.items()},
        identity_by_phone={key: tuple(value) for key, value in identity_by_phone.items()},
        messages_by_customer={key: tuple(value) for key, value in messages_by_customer.items()},
        orders_by_phone={key: tuple(value) for key, value in orders_by_phone.items()},
    )


def _scope_feature_inputs(
    index: _FeatureInputIndex,
    target_customers: Sequence[str],
) -> Tuple[List[Mapping[str, object]], List[CanonicalOrder], List[Message]]:
    """Return the connected customer/phone component for the requested rows.

    Historical cards normally request one customer.  Scoping avoids repeatedly
    scanning the entire corpus while retaining shared-phone de-duplication and
    identity-conflict semantics for every connected customer.
    """

    customers = {str(value) for value in target_customers if str(value)}
    pending = list(customers)
    phones = set()
    while pending:
        customer_key = pending.pop()
        for row in index.identity_by_customer.get(customer_key, ()):
            if str(row.get("state") or "").lower() != "approved":
                continue
            phone_hmac = str(row.get("phone_hmac") or "")
            if not phone_hmac or phone_hmac in phones:
                continue
            phones.add(phone_hmac)
            for linked in index.identity_by_phone.get(phone_hmac, ()):
                if str(linked.get("state") or "").lower() != "approved":
                    continue
                linked_customer = str(linked.get("customer_key") or "")
                if linked_customer and linked_customer not in customers:
                    customers.add(linked_customer)
                    pending.append(linked_customer)

    scoped_identity: List[Mapping[str, object]] = []
    seen_identity_rows = set()
    for customer_key in sorted(customers):
        for row in index.identity_by_customer.get(customer_key, ()):
            marker = id(row)
            if marker not in seen_identity_rows:
                seen_identity_rows.add(marker)
                scoped_identity.append(row)
    scoped_messages = [
        item
        for customer_key in sorted(customers)
        for item in index.messages_by_customer.get(customer_key, ())
    ]
    scoped_orders = [
        item
        for phone_hmac in sorted(phones)
        for item in index.orders_by_phone.get(phone_hmac, ())
    ]
    return scoped_identity, scoped_orders, scoped_messages


def _moment(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("%s must be a valid ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s must include a timezone" % field)
    return parsed.astimezone(SHANGHAI)


def _iso(value: datetime) -> str:
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _optional_moment(value: object, *, field: str) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    return _moment(value, field=field)


def _safe_json(value: object, default: object) -> object:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _bool_db(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


def _action_schema_complete(connection) -> bool:
    """Return whether the additive v3 queue migration is actually present.

    Some reviewed run databases may already carry ``user_version=3`` from an
    earlier v3 build while missing tables or columns added later under that
    same version.  Inspect the concrete schema without running the migration;
    this keeps fully upgraded idempotent builds from touching ``build_meta``.
    """

    required_tables = {
        "customer_value_snapshots",
        "card_feature_snapshots",
        "action_annotations",
        "card_annotations",
        "strategy_catalog",
        "action_queue_items",
        "action_queue_runs",
        "action_queue_feedback",
        "contact_suppressions",
    }
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not required_tables.issubset(existing_tables):
        return False
    item_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(action_queue_items)")
    }
    order_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(orders)")
    }
    card_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(decision_cards)")
    }
    return (
        {"signals_json", "missing_facts_json"}.issubset(item_columns)
        and {
            "sku_name",
            "factory",
            "category",
            "color",
            "size",
            "ordered_at",
            "paid_at",
            "order_note",
        }.issubset(order_columns)
        and "boundary_message_key" in card_columns
    )


def _load_profile_sources(connection, requested_profile: Optional[str]) -> List[_ProfileSource]:
    parameters: Tuple[object, ...] = ()
    where = ""
    if requested_profile:
        where = "WHERE cr.profile_id=?"
        parameters = (requested_profile,)
    rows = list(
        connection.execute(
            """
            SELECT cr.customer_key,cr.profile_id,cr.source_snapshot_id,
                   ss.run_id,ss.consistency_state AS snapshot_consistency,
                   po.observed_until,po.initialized,po.last_error_code,
                   po.consistency_state AS observation_consistency,
                   ar.state AS account_state
            FROM conversation_refs cr
            JOIN source_snapshots ss ON ss.snapshot_id=cr.source_snapshot_id
            JOIN account_registry ar ON ar.profile_id=cr.profile_id
            LEFT JOIN profile_observations po
              ON po.snapshot_id=cr.source_snapshot_id AND po.profile_id=cr.profile_id
            %s
            ORDER BY cr.profile_id,cr.customer_key
            """
            % where,
            parameters,
        )
    )
    grouped: Dict[str, List[object]] = defaultdict(list)
    for row in rows:
        grouped[str(row["profile_id"])].append(row)
    if requested_profile and requested_profile not in grouped:
        raise RuntimeError("requested profile has no normalized conversations")
    if not grouped:
        raise RuntimeError("database has no normalized profile conversations")

    output: List[_ProfileSource] = []
    for profile_id in sorted(grouped):
        profile_rows = grouped[profile_id]
        run_ids = {str(row["run_id"]) for row in profile_rows}
        if len(run_ids) != 1:
            raise RuntimeError("profile conversations span multiple pipeline runs")
        account_states = {str(row["account_state"]) for row in profile_rows}
        snapshot_by_customer: Dict[str, str] = {}
        observed_by_customer: Dict[str, Optional[datetime]] = {}
        observed_values: List[datetime] = []
        healthy = account_states == {"approved"}
        for row in profile_rows:
            customer_key = str(row["customer_key"])
            snapshot_by_customer[customer_key] = str(row["source_snapshot_id"])
            observed = _optional_moment(
                row["observed_until"], field="profile observed_until"
            )
            observed_by_customer[customer_key] = observed
            if observed is not None:
                observed_values.append(observed)
            if (
                int(row["initialized"] or 0) != 1
                or row["last_error_code"] is not None
                or str(row["snapshot_consistency"] or "") != "consistent"
                or str(row["observation_consistency"] or "") != "consistent"
                or observed is None
            ):
                healthy = False
        output.append(
            _ProfileSource(
                profile_id=profile_id,
                run_id=next(iter(run_ids)),
                account_state=next(iter(account_states)) if len(account_states) == 1 else "review",
                customer_keys=tuple(str(row["customer_key"]) for row in profile_rows),
                snapshot_by_customer=snapshot_by_customer,
                observed_by_customer=observed_by_customer,
                profile_observed_until=min(observed_values) if observed_values else None,
                source_healthy=healthy,
            )
        )
    return output


def _load_messages(
    connection,
    source: _ProfileSource,
    *,
    as_of_at: datetime,
) -> List[Message]:
    rows = list(
        connection.execute(
            """
            SELECT m.message_key,m.customer_key,m.role,m.timestamp,m.text,
                   m.source_file,m.source_ordinal
            FROM messages m
            JOIN conversation_refs cr ON cr.customer_key=m.customer_key
            WHERE cr.profile_id=?
            ORDER BY m.timestamp,m.source_ordinal,m.message_key
            """,
            (source.profile_id,),
        )
    )
    output: List[Message] = []
    for row in rows:
        at = _moment(row["timestamp"], field="message timestamp")
        observed = source.observed_by_customer.get(str(row["customer_key"]))
        if at > as_of_at or (observed is not None and at > observed):
            continue
        output.append(
            Message(
                message_key=str(row["message_key"]),
                customer_key=str(row["customer_key"]),
                role=str(row["role"]),
                timestamp=_iso(at),
                text=str(row["text"]),
                source_file=str(row["source_file"]),
                source_ordinal=int(row["source_ordinal"]),
            )
        )
    return output


def _load_identity_rows(connection, profile_id: str) -> List[Dict[str, object]]:
    return [
        {
            "customer_key": str(row["customer_key"]),
            "phone_hmac": str(row["phone_hmac"] or ""),
            "state": str(row["state"]),
        }
        for row in connection.execute(
            """
            SELECT cl.customer_key,cl.phone_hmac,cl.state
            FROM conversation_links cl
            JOIN conversation_refs cr ON cr.customer_key=cl.customer_key
            WHERE cr.profile_id=?
            ORDER BY cl.customer_key,cl.link_id
            """,
            (profile_id,),
        )
    ]


def _load_outcome_links(connection, profile_id: str) -> List[ConversationLink]:
    return [
        ConversationLink(
            customer_key=str(row["customer_key"]),
            phone_hmac=str(row["phone_hmac"]) if row["phone_hmac"] else None,
            state=str(row["state"]),
            eligibility=str(row["eligibility"] or "order_ineligible"),
        )
        for row in connection.execute(
            """
            SELECT cl.customer_key,cl.phone_hmac,cl.state,coe.eligibility
            FROM conversation_links cl
            JOIN conversation_refs cr ON cr.customer_key=cl.customer_key
            LEFT JOIN conversation_order_eligibility coe
              ON coe.customer_key=cl.customer_key
            WHERE cr.profile_id=?
            ORDER BY cl.customer_key,cl.link_id
            """,
            (profile_id,),
        )
    ]


def _canonical_order(row) -> CanonicalOrder:
    flags = _safe_json(row["quality_flags_json"], [])
    if not isinstance(flags, list):
        flags = []
    return CanonicalOrder(
        order_line_id=str(row["order_line_id"]),
        source_namespace=str(row["source_namespace"]),
        record_id=str(row["record_id"]),
        phone_hmac=str(row["phone_hmac"]) if row["phone_hmac"] else None,
        ordered_at=str(row["ordered_at"]) if row["ordered_at"] else None,
        paid_at=str(row["paid_at"]) if row["paid_at"] else None,
        paid_on=str(row["paid_on"]) if row["paid_on"] else None,
        revenue_minor=int(row["revenue_minor"]) if row["revenue_minor"] is not None else None,
        currency=str(row["currency"]),
        platform=str(row["platform"]) if row["platform"] else None,
        refund_type=str(row["refund_type"]) if row["refund_type"] else None,
        refund_reason=str(row["refund_reason"]) if row["refund_reason"] else None,
        refund_amount_minor=(
            int(row["refund_amount_minor"])
            if row["refund_amount_minor"] is not None
            else None
        ),
        refund_on=str(row["refund_on"]) if row["refund_on"] else None,
        return_status=str(row["return_status"]) if row["return_status"] else None,
        source_hash=str(row["source_hash"]),
        quality_flags=tuple(str(item) for item in flags),
        sku_name=str(row["sku_name"]) if row["sku_name"] else None,
        factory=str(row["factory"]) if row["factory"] else None,
        category=str(row["category"]) if row["category"] else None,
        color=str(row["color"]) if row["color"] else None,
        size=str(row["size"]) if row["size"] else None,
        order_note=str(row["order_note"]) if row["order_note"] else None,
    )


def _load_order_source(connection) -> Optional[_OrderSource]:
    snapshot = connection.execute(
        """
        SELECT os.order_snapshot_id,os.synced_at,ss.run_id,ss.consistency_state
        FROM order_snapshots os
        JOIN source_snapshots ss ON ss.snapshot_id=os.source_snapshot_id
        WHERE os.state='active'
        LIMIT 1
        """
    ).fetchone()
    if snapshot is None or str(snapshot["consistency_state"]) != "consistent":
        return None
    rows = tuple(
        _canonical_order(row)
        for row in connection.execute(
            "SELECT * FROM orders WHERE order_snapshot_id=? ORDER BY order_line_id",
            (snapshot["order_snapshot_id"],),
        )
    )
    return _OrderSource(
        order_snapshot_id=str(snapshot["order_snapshot_id"]),
        run_id=str(snapshot["run_id"]),
        synced_at=_moment(snapshot["synced_at"], field="order synced_at"),
        rows=rows,
    )


def _truncate_orders(orders: Sequence[CanonicalOrder], as_of_at: datetime) -> List[CanonicalOrder]:
    output: List[CanonicalOrder] = []
    as_of_day = as_of_at.astimezone(SHANGHAI).date()
    for item in orders:
        if item.paid_at:
            try:
                paid_at = _moment(item.paid_at, field="paid_at")
            except ValueError:
                paid_at = None
            if paid_at is not None and paid_at > as_of_at:
                continue
        elif item.paid_on:
            try:
                paid_on = date.fromisoformat(item.paid_on[:10])
            except ValueError:
                paid_on = None
            if paid_on is not None and paid_on > as_of_day:
                continue
        truncated = item
        if item.refund_on:
            try:
                refund_on = date.fromisoformat(item.refund_on[:10])
            except ValueError:
                refund_on = None
            if refund_on is not None and refund_on > as_of_day:
                truncated = replace(
                    item,
                    refund_type=None,
                    refund_reason=None,
                    refund_amount_minor=None,
                    refund_on=None,
                    return_status=None,
                )
        output.append(truncated)
    return output


def _identity_state(
    customer_key: str,
    identity_rows: Sequence[Mapping[str, object]],
    returned_profiles: Mapping[str, object],
) -> str:
    customer_rows = [row for row in identity_rows if row["customer_key"] == customer_key]
    approved_phones = {
        str(row["phone_hmac"])
        for row in customer_rows
        if row["state"] == "approved" and row["phone_hmac"]
    }
    if any(row["state"] == "conflict" for row in customer_rows) or len(approved_phones) > 1:
        return "conflict"
    if customer_key in returned_profiles:
        return "approved"
    if len(approved_phones) == 1:
        return "shared_phone_secondary"
    return "unverified"


def _fallback_profile(
    customer_key: str,
    *,
    profile_id: str,
    as_of_at: datetime,
    identity_state: str,
    messages: Sequence[Message],
) -> Dict[str, object]:
    visible = [
        item
        for item in messages
        if item.customer_key == customer_key
        and _moment(item.timestamp, field="message timestamp") <= as_of_at
    ]
    customer_messages = [item for item in visible if item.role == "customer"]
    hour_counts = [0] * 24
    active_days = set()
    for item in customer_messages:
        at = _moment(item.timestamp, field="message timestamp")
        hour_counts[at.hour] += 1
        active_days.add(at.date())
    return {
        "customer_key": customer_key,
        "profile_id": profile_id,
        "as_of_at": _iso(as_of_at),
        "feature_rule_version": FEATURE_RULE_VERSION,
        "identity_state": identity_state,
        "linked_customer_count": 1,
        "day_of_month_bucket": day_of_month_bucket(as_of_at),
        "customer_message_count": len(customer_messages),
        "active_day_count": len(active_days),
        "active_hour_counts": hour_counts,
        "recommended_contact_window": MANUAL_CONTACT_WINDOW,
        "contact_window_basis": "identity_unverified",
        "contact_window_evidence_count": 0,
        "contact_window_confidence": None,
        "order_features_available": False,
        "rfm_recency_days": None,
        "rfm_frequency": None,
        "rfm_monetary_minor": None,
        "value_bucket": "unavailable",
        "median_repurchase_interval_days": None,
        "aftersales_count": None,
        "aftersales_rate": None,
        "aftersales_risk": "unavailable",
        "preferred_skus": [],
        "preferred_factories": [],
        "preferred_categories": [],
        "preferred_colors": [],
        "preferred_sizes": [],
        "unknown_aftersales_count": 0,
        "quality_flags": [identity_state],
    }


def _feature_rows_at(
    source: _ProfileSource,
    *,
    cutoff: datetime,
    target_customers: Sequence[str],
    identity_rows: Sequence[Mapping[str, object]],
    orders: Sequence[CanonicalOrder],
    messages: Sequence[Message],
    order_source: Optional[_OrderSource],
    effective_collector_status: str,
    secret: str,
    input_index: Optional[_FeatureInputIndex] = None,
) -> Tuple[Dict[str, Dict[str, object]], Dict[str, object]]:
    observation = source.profile_observed_until
    visible_observation = min(observation, cutoff) if observation is not None else None
    order_available = bool(
        order_source is not None
        and order_source.run_id == source.run_id
        and order_source.synced_at <= cutoff
    )
    feature_index = input_index or _index_feature_inputs(identity_rows, orders, messages)
    scoped_identity, scoped_orders, scoped_messages = _scope_feature_inputs(
        feature_index, target_customers
    )
    eligible_orders = scoped_orders if order_available else []
    order_synced_at = order_source.synced_at if order_available and order_source else None
    built: CustomerFeatureSnapshot = build_customer_profiles(
        [row for row in scoped_identity if row.get("phone_hmac")],
        eligible_orders,
        scoped_messages,
        as_of_at=cutoff,
        message_observed_until=visible_observation,
        order_synced_at=order_synced_at,
        collector_status=effective_collector_status,
    )
    freshness = asdict(built.freshness)
    profile_values: Dict[str, Dict[str, object]] = {}
    returned_profiles = {item.customer_key: item for item in built.profiles}
    for customer_key in target_customers:
        profile = returned_profiles.get(customer_key)
        if profile is not None:
            payload = asdict(profile)
            payload["profile_id"] = source.profile_id
            payload["identity_state"] = "approved"
        else:
            payload = _fallback_profile(
                customer_key,
                profile_id=source.profile_id,
                as_of_at=cutoff,
                identity_state=_identity_state(
                    customer_key, scoped_identity, returned_profiles
                ),
                messages=scoped_messages,
            )
        feature_snapshot_id = hmac_id(
            secret,
            "feature-snapshot",
            PIPELINE_VERSION,
            source.run_id,
            source.profile_id,
            customer_key,
            _iso(cutoff),
            FEATURE_RULE_VERSION,
        )
        profile_values[customer_key] = {
            "feature_snapshot_id": feature_snapshot_id,
            "run_id": source.run_id,
            "customer_key": customer_key,
            "profile_id": source.profile_id,
            "as_of_at": _iso(cutoff),
            "message_snapshot_id": source.snapshot_by_customer[customer_key],
            "order_snapshot_id": (
                order_source.order_snapshot_id if order_available and order_source else None
            ),
            "feature_rule_version": FEATURE_RULE_VERSION,
            "profile": payload,
            "freshness": freshness,
            "messages_fresh": bool(built.freshness.messages_fresh),
            "orders_fresh": bool(built.freshness.orders_fresh),
            "queue_ready": bool(built.freshness.queue_ready),
        }
    return profile_values, freshness


def _customer_signal(text: str) -> str:
    normalized = str(text or "")
    positive = any(marker in normalized for marker in _POSITIVE_MARKERS)
    negative = any(marker in normalized for marker in _NEGATIVE_MARKERS)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "unknown"


def _audit_annotation(card: DecisionCard) -> Dict[str, object]:
    customer_text = "\n".join(
        str(item.get("text") or "")
        for item in card.blind_context
        if item.get("role") == "customer"
    )
    action_text = str(card.observed_action.get("text") or "")
    signal = _customer_signal(customer_text)
    if card.card_type == "proactive_followup":
        strategy = "light_followup"
    elif any(marker in customer_text for marker in _AFTERSALES_MARKERS):
        strategy = "aftersales_repair"
    elif any(marker in customer_text for marker in ("价格", "多少钱", "优惠", "便宜")):
        strategy = "quote"
    elif any(marker in customer_text for marker in ("推荐", "适合", "选哪", "款式", "尺码")):
        strategy = "recommend"
    elif any(marker in customer_text for marker in ("正品", "靠谱吗", "质量", "证明")):
        strategy = "trust_proof"
    elif any(marker in customer_text for marker in ("？", "?", "吗", "怎么", "什么时候")):
        strategy = "answer_fact"
    else:
        strategy = "clarify"

    required_by_strategy = {
        "answer_fact": ("customer_request",),
        "clarify": ("customer_request",),
        "recommend": ("customer_request", "product_category"),
        "quote": ("current_price",),
        "trust_proof": ("customer_request",),
        "light_followup": (),
        "aftersales_repair": ("aftersales_policy", "order_status"),
    }
    reasons: List[str] = []
    action_state = str(card.observed_action.get("state") or "unobserved")
    if not action_text:
        reuse_status = "prohibited"
        reasons.append("no_observed_reply")
    elif any(marker in action_text for marker in _GUARANTEE_MARKERS):
        reuse_status = "prohibited"
        reasons.append("unsafe_guarantee")
    elif strategy == "aftersales_repair":
        reuse_status = "case_only"
        reasons.append("aftersales_case_specific")
    elif strategy in {"answer_fact", "recommend", "quote", "clarify"}:
        reuse_status = "fill_slots"
        reasons.append("dynamic_facts_required")
    else:
        reuse_status = "direct"
        reasons.append("generic_reviewed_structure")
    return {
        "customer_signal": signal,
        "reply_strategy": strategy,
        "reuse_status": reuse_status,
        "required_facts": list(required_by_strategy[strategy]),
        "prohibited_claims": list(PROHIBITED_CLAIMS),
        "annotation": {
            "observed_action_state": action_state,
            "reason_codes": reasons,
            "outcome_fields_used": False,
        },
    }


def _value_score(profile: Mapping[str, object]) -> Optional[float]:
    return {
        "none": 0.0,
        "low": 25.0,
        "medium": 50.0,
        "high": 75.0,
        "vip": 100.0,
    }.get(str(profile.get("value_bucket") or ""))


def _repurchase(profile: Mapping[str, object]) -> Tuple[Optional[float], bool]:
    recency = profile.get("rfm_recency_days")
    interval = profile.get("median_repurchase_interval_days")
    frequency = profile.get("rfm_frequency")
    if recency is None or interval is None or frequency is None:
        return None, False
    try:
        recency_value = float(recency)
        interval_value = float(interval)
        frequency_value = int(frequency)
    except (TypeError, ValueError):
        return None, False
    if frequency_value < 2 or interval_value <= 0:
        return None, False
    ratio = recency_value / interval_value
    due = 0.8 <= ratio <= 1.5
    score = max(0.0, min(100.0, 100.0 - abs(1.0 - ratio) * 80.0))
    return round(score, 3), due


def _approved_phone_state(
    identity_rows: Sequence[Mapping[str, object]], customer_key: str
) -> Tuple[Optional[str], bool]:
    rows = [row for row in identity_rows if row["customer_key"] == customer_key]
    phones = {
        str(row["phone_hmac"])
        for row in rows
        if row["state"] == "approved" and row["phone_hmac"]
    }
    conflict = any(row["state"] == "conflict" for row in rows) or len(phones) > 1
    return (next(iter(phones)) if len(phones) == 1 else None), conflict


def _feedback_history(connection, profile_id: str, as_of_at: datetime) -> Dict[str, List[datetime]]:
    output: Dict[str, List[datetime]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT aq.customer_key,aqf.created_at
        FROM action_queue_feedback aqf
        JOIN action_queue_items aq ON aq.action_id=aqf.action_id
        WHERE aq.profile_id=? AND aq.lane='proactive_today'
          AND aq.human_confirmation_state IN ('adopted','edited')
          AND aqf.outcome IN ('adopted','edited')
          AND aqf.rowid=(
              SELECT latest.rowid FROM action_queue_feedback latest
              WHERE latest.action_id=aqf.action_id
              ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1
          )
        ORDER BY aqf.created_at DESC
        """,
        (profile_id,),
    ):
        at = _moment(row["created_at"], field="feedback created_at")
        if at <= as_of_at:
            output[str(row["customer_key"])].append(at)
    return output


def _active_contact_suppressions(
    connection,
    source: _ProfileSource,
    messages: Sequence[Message],
    *,
    as_of_at: datetime,
    secret: str,
) -> Tuple[Dict[str, set], Dict[str, set]]:
    """Persist strong refusals and return all active local suppressions."""

    for item in messages:
        if item.role != "customer" or not any(
            pattern.search(item.text) for pattern in _CONTACT_REJECTION_PATTERNS
        ):
            continue
        starts_at = _moment(item.timestamp, field="suppression message timestamp")
        suppression_id = hmac_id(
            secret,
            "contact-suppression",
            source.profile_id,
            item.customer_key,
            item.message_key,
            "explicit_rejection",
        )
        connection.execute(
            """
            INSERT INTO contact_suppressions(
                suppression_id,customer_key,profile_id,phone_hmac,reason_code,
                starts_at,ends_at,source_action_id,created_at
            ) VALUES(?,?,?,NULL,'explicit_rejection',?,NULL,NULL,?)
            ON CONFLICT(suppression_id) DO NOTHING
            """,
            (
                suppression_id,
                item.customer_key,
                source.profile_id,
                _iso(starts_at),
                _iso(as_of_at),
            ),
        )

    by_customer: Dict[str, set] = defaultdict(set)
    by_phone: Dict[str, set] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT customer_key,phone_hmac,reason_code,starts_at,ends_at
        FROM contact_suppressions
        WHERE profile_id=?
        """,
        (source.profile_id,),
    ):
        starts_at = _moment(row["starts_at"], field="suppression starts_at")
        ends_at = _optional_moment(row["ends_at"], field="suppression ends_at")
        if starts_at > as_of_at or (ends_at is not None and ends_at <= as_of_at):
            continue
        reason = str(row["reason_code"] or "manual_suppression").strip().lower()
        by_customer[str(row["customer_key"])].add(reason)
        if row["phone_hmac"]:
            by_phone[str(row["phone_hmac"])].add(reason)
    return by_customer, by_phone


def _candidate_rows(
    connection,
    source: _ProfileSource,
    *,
    as_of_at: datetime,
    current_features: Mapping[str, Mapping[str, object]],
    identity_rows: Sequence[Mapping[str, object]],
    messages: Sequence[Message],
    orders: Sequence[CanonicalOrder],
    secret: str,
) -> List[ActionCandidate]:
    by_customer: Dict[str, List[Message]] = defaultdict(list)
    for item in messages:
        by_customer[item.customer_key].append(item)
    for values in by_customer.values():
        values.sort(
            key=lambda item: (
                _moment(item.timestamp, field="message timestamp"),
                item.source_ordinal,
                item.message_key,
            )
        )
    feedback = _feedback_history(connection, source.profile_id, as_of_at)
    suppressions_by_customer, suppressions_by_phone = _active_contact_suppressions(
        connection,
        source,
        messages,
        as_of_at=as_of_at,
        secret=secret,
    )
    orders_by_phone: Dict[str, List[CanonicalOrder]] = defaultdict(list)
    for item in orders:
        if item.phone_hmac:
            orders_by_phone[item.phone_hmac].append(item)

    candidates: List[ActionCandidate] = []
    for customer_key in source.customer_keys:
        values = by_customer.get(customer_key, [])
        latest = values[-1] if values else None
        latest_customer = next(
            (item for item in reversed(values) if item.role == "customer"), None
        )
        latest_text = latest_customer.text if latest_customer else ""
        unresolved = bool(latest is not None and latest.role == "customer")
        signal = _customer_signal(latest_text)
        phone_hmac, identity_conflict = _approved_phone_state(
            identity_rows, customer_key
        )
        active_suppressions = set(suppressions_by_customer.get(customer_key, set()))
        if phone_hmac:
            active_suppressions.update(suppressions_by_phone.get(phone_hmac, set()))
        feature_row = current_features[customer_key]
        profile = feature_row["profile"]
        assert isinstance(profile, Mapping)
        value_score = _value_score(profile)
        repurchase_score, repurchase_due = _repurchase(profile)
        phone_orders = orders_by_phone.get(phone_hmac or "", [])
        aftersales_open = any(marker in latest_text for marker in _AFTERSALES_MARKERS)
        aftersales_open = aftersales_open or any(
            "aftersale_open" in item.quality_flags
            or (
                item.refund_type in {"return", "return_taro", "exchange"}
                and str(item.return_status or "").strip().lower()
                not in COMPLETED_RETURN_STATES
            )
            for item in phone_orders
        )
        recent_days: List[int] = []
        for item in phone_orders:
            if not item.paid_on or item.revenue_minor is None or item.revenue_minor <= 0:
                continue
            try:
                paid_on = date.fromisoformat(item.paid_on[:10])
            except ValueError:
                continue
            days = (as_of_at.date() - paid_on).days
            if days >= 0:
                recent_days.append(days)
        recently_ordered = bool(recent_days and min(recent_days) <= 7 and not unresolved)
        preferred_hour = None
        observations = 0
        hours = profile.get("active_hour_counts")
        if (
            profile.get("contact_window_basis") == "wechat_customer_messages"
            and isinstance(hours, (list, tuple))
            and len(hours) == 24
        ):
            numeric_hours = [int(value or 0) for value in hours]
            preferred_hour = max(range(24), key=lambda hour: (numeric_hours[hour], -hour))
            observations = int(profile.get("contact_window_evidence_count") or 0)

        contact_times = feedback.get(customer_key, [])
        last_proactive_at = contact_times[0] if contact_times else None
        consecutive_no_reply = 0
        for contact_at in contact_times:
            replied = any(
                item.role == "customer"
                and _moment(item.timestamp, field="message timestamp") > contact_at
                for item in values
            )
            if replied:
                break
            consecutive_no_reply += 1

        order_facts_ready = bool(
            feature_row["orders_fresh"]
            and profile.get("identity_state") == "approved"
            and int(profile.get("rfm_frequency") or 0) > 0
        )
        facts_sufficient = bool(latest_customer) if unresolved else order_facts_ready
        required_facts = ("customer_request",) if unresolved else ()
        available_facts: List[str] = []
        if latest_customer:
            available_facts.append("customer_request")
        if profile.get("preferred_categories"):
            available_facts.append("product_category")
        if profile.get("preferred_skus"):
            available_facts.append("product_code")
        product_score = (
            70.0
            if feature_row["orders_fresh"]
            and (profile.get("preferred_skus") or profile.get("preferred_categories"))
            else None
        )
        candidates.append(
            ActionCandidate(
                customer_key=customer_key,
                profile_id=source.profile_id,
                phone_hmac=phone_hmac,
                unresolved_inbound=unresolved,
                proactive_eligible=bool(repurchase_due and order_facts_ready and not unresolved),
                value_score=value_score,
                repurchase_score=repurchase_score,
                intent_signal=signal,
                product_candidate_score=product_score,
                aftersales_open=aftersales_open,
                explicit_rejection=(
                    "explicit_rejection" in active_suppressions
                    or any(marker in latest_text for marker in _NEGATIVE_MARKERS)
                ),
                manual_suppression=bool(active_suppressions - {"explicit_rejection"}),
                recently_ordered=recently_ordered,
                consecutive_no_reply=consecutive_no_reply,
                identity_conflict=identity_conflict,
                facts_sufficient=facts_sufficient,
                required_fact_codes=required_facts,
                available_fact_codes=tuple(sorted(set(available_facts))),
                last_proactive_at=last_proactive_at,
                preferred_contact_hour=preferred_hour,
                active_hour_observations=observations,
            )
        )
    return candidates


def _unknown_outcomes(
    cards: Sequence[DecisionCard],
    links: Sequence[ConversationLink],
    *,
    computed_at: datetime,
) -> Dict[str, CardOutcome]:
    verified = {
        item.customer_key
        for item in links
        if item.state == "approved"
        and item.eligibility in {"order_customer", "album_customer"}
        and item.phone_hmac
    }
    return {
        card.card_id: CardOutcome(
            card_id=card.card_id,
            paid_1d=None,
            paid_3d=None,
            paid_7d=None,
            retained_30d=None,
            aftersale_30d=None,
            exchange_30d=None,
            compensation_30d=None,
            refund_loss_ratio=None,
            attribution_state=(
                "quality_unknown" if card.customer_key in verified else "identity_unverified"
            ),
            attribution_flags=("order_snapshot_unavailable",),
            matched_orders=(),
            computed_at=_iso(computed_at),
        )
        for card in cards
    }


def _persist_feature(connection, row: Mapping[str, object], created_at: str) -> None:
    connection.execute(
        """
        INSERT INTO customer_value_snapshots(
            feature_snapshot_id,run_id,customer_key,profile_id,as_of_at,
            message_snapshot_id,order_snapshot_id,feature_rule_version,profile_json,
            freshness_json,messages_fresh,orders_fresh,queue_ready,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(feature_snapshot_id) DO UPDATE SET
            profile_json=excluded.profile_json,freshness_json=excluded.freshness_json,
            messages_fresh=excluded.messages_fresh,orders_fresh=excluded.orders_fresh,
            queue_ready=excluded.queue_ready
        """,
        (
            row["feature_snapshot_id"],
            row["run_id"],
            row["customer_key"],
            row["profile_id"],
            row["as_of_at"],
            row["message_snapshot_id"],
            row["order_snapshot_id"],
            row["feature_rule_version"],
            json_dumps(row["profile"]),
            json_dumps(row["freshness"]),
            1 if row["messages_fresh"] else 0,
            1 if row["orders_fresh"] else 0,
            1 if row["queue_ready"] else 0,
            created_at,
        ),
    )


def _persist_card(connection, card: DecisionCard, created_at: str) -> None:
    connection.execute(
        """
        INSERT INTO decision_cards(
            card_id,customer_key,episode_id,card_type,as_of_at,boundary_ordinal,
            boundary_message_key,source_snapshot_id,action_window_end,observation_until,
            blind_context_json,observed_action_json,context_message_keys_json,
            action_message_keys_json,split,review_status,rule_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)
        ON CONFLICT(card_id) DO UPDATE SET
            episode_id=excluded.episode_id,card_type=excluded.card_type,
            as_of_at=excluded.as_of_at,boundary_ordinal=excluded.boundary_ordinal,
            boundary_message_key=excluded.boundary_message_key,
            source_snapshot_id=excluded.source_snapshot_id,
            action_window_end=excluded.action_window_end,
            observation_until=excluded.observation_until,
            blind_context_json=excluded.blind_context_json,
            observed_action_json=excluded.observed_action_json,
            context_message_keys_json=excluded.context_message_keys_json,
            action_message_keys_json=excluded.action_message_keys_json,
            split=excluded.split,rule_version=excluded.rule_version
        """,
        (
            card.card_id,
            card.customer_key,
            card.episode_id,
            card.card_type,
            card.as_of_at,
            card.boundary_ordinal,
            card.boundary_message_key,
            card.source_snapshot_id,
            card.action_window_end,
            card.observation_until,
            json_dumps(card.blind_context),
            json_dumps(card.observed_action),
            json_dumps(list(card.context_message_keys)),
            json_dumps(list(card.action_message_keys)),
            card.split,
            card.rule_version,
            created_at,
        ),
    )


def _persist_outcome(connection, outcome: CardOutcome) -> None:
    connection.execute(
        """
        INSERT INTO card_outcomes(
            card_id,paid_1d,paid_3d,paid_7d,retained_30d,aftersale_30d,
            exchange_30d,compensation_30d,refund_loss_ratio,attribution_state,
            attribution_flags_json,matched_orders_json,computed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(card_id) DO UPDATE SET
            paid_1d=excluded.paid_1d,paid_3d=excluded.paid_3d,
            paid_7d=excluded.paid_7d,retained_30d=excluded.retained_30d,
            aftersale_30d=excluded.aftersale_30d,exchange_30d=excluded.exchange_30d,
            compensation_30d=excluded.compensation_30d,
            refund_loss_ratio=excluded.refund_loss_ratio,
            attribution_state=excluded.attribution_state,
            attribution_flags_json=excluded.attribution_flags_json,
            matched_orders_json=excluded.matched_orders_json,
            computed_at=excluded.computed_at
        """,
        (
            outcome.card_id,
            _bool_db(outcome.paid_1d),
            _bool_db(outcome.paid_3d),
            _bool_db(outcome.paid_7d),
            _bool_db(outcome.retained_30d),
            _bool_db(outcome.aftersale_30d),
            _bool_db(outcome.exchange_30d),
            _bool_db(outcome.compensation_30d),
            outcome.refund_loss_ratio,
            outcome.attribution_state,
            json_dumps(list(outcome.attribution_flags)),
            json_dumps(list(outcome.matched_orders)),
            outcome.computed_at,
        ),
    )


def _persist_queue(
    connection,
    source: _ProfileSource,
    queue: Mapping[str, object],
    candidates: Sequence[ActionCandidate],
    current_features: Mapping[str, Mapping[str, object]],
    *,
    created_at: str,
    secret: str,
) -> None:
    queue_run_id = hmac_id(
        secret,
        "queue-run",
        PIPELINE_VERSION,
        source.profile_id,
        queue["queue_date"],
    )
    connection.execute(
        """
        INSERT INTO action_queue_runs(
            queue_run_id,run_id,profile_id,queue_date,as_of_at,status,
            policy_version,block_reasons_json,freshness_json,counts_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(profile_id,queue_date) DO UPDATE SET
            run_id=excluded.run_id,as_of_at=excluded.as_of_at,status=excluded.status,
            policy_version=excluded.policy_version,
            block_reasons_json=excluded.block_reasons_json,
            freshness_json=excluded.freshness_json,counts_json=excluded.counts_json
        """,
        (
            queue_run_id,
            source.run_id,
            source.profile_id,
            queue["queue_date"],
            queue["generated_at"],
            queue["status"],
            queue["policy_version"],
            json_dumps(queue["block_reasons"]),
            json_dumps(queue["freshness"]),
            json_dumps(queue["counts"]),
            created_at,
        ),
    )
    candidate_by_customer = {item.customer_key: item for item in candidates}
    lanes = queue["lanes"]
    assert isinstance(lanes, Mapping)
    for lane in ("reply_now", "proactive_today", "suppressed"):
        lane_items = lanes[lane]
        assert isinstance(lane_items, list)
        for item in lane_items:
            customer_key = str(item["customer_key"])
            candidate = candidate_by_customer[customer_key]
            existing = connection.execute(
                """
                SELECT action_id FROM action_queue_items
                WHERE profile_id=? AND queue_date=? AND customer_key=?
                """,
                (source.profile_id, queue["queue_date"], customer_key),
            ).fetchone()
            action_id = str(existing["action_id"]) if existing else str(item["action_id"])
            confidence = {"high": 0.9, "medium": 0.65, "low": 0.4}.get(
                str(item.get("confidence") or ""), 0.5
            )
            connection.execute(
                """
                INSERT INTO action_queue_items(
                    action_id,run_id,feature_snapshot_id,customer_key,profile_id,
                    queue_date,lane,priority_score,priority_version,phone_hmac,
                    reason_codes_json,contact_window_json,recommended_action,
                    strategy_version,signals_json,required_facts_json,
                    missing_facts_json,prohibited_claims_json,draft_json,confidence,
                    freshness_json,human_confirmation_state,send_allowed,created_at,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,?)
                ON CONFLICT(profile_id,queue_date,customer_key) DO UPDATE SET
                    run_id=excluded.run_id,feature_snapshot_id=excluded.feature_snapshot_id,
                    lane=excluded.lane,priority_score=excluded.priority_score,
                    priority_version=excluded.priority_version,phone_hmac=excluded.phone_hmac,
                    reason_codes_json=excluded.reason_codes_json,
                    contact_window_json=excluded.contact_window_json,
                    recommended_action=excluded.recommended_action,
                    strategy_version=excluded.strategy_version,
                    signals_json=excluded.signals_json,
                    required_facts_json=excluded.required_facts_json,
                    missing_facts_json=excluded.missing_facts_json,
                    prohibited_claims_json=excluded.prohibited_claims_json,
                    draft_json=excluded.draft_json,confidence=excluded.confidence,
                    freshness_json=excluded.freshness_json,send_allowed=0,
                    human_confirmation_state=CASE WHEN
                        action_queue_items.run_id=excluded.run_id AND
                        action_queue_items.feature_snapshot_id=excluded.feature_snapshot_id AND
                        action_queue_items.lane=excluded.lane AND
                        action_queue_items.priority_version=excluded.priority_version AND
                        action_queue_items.reason_codes_json=excluded.reason_codes_json AND
                        action_queue_items.contact_window_json=excluded.contact_window_json AND
                        action_queue_items.recommended_action=excluded.recommended_action AND
                        action_queue_items.strategy_version=excluded.strategy_version AND
                        action_queue_items.signals_json=excluded.signals_json AND
                        action_queue_items.required_facts_json=excluded.required_facts_json AND
                        action_queue_items.missing_facts_json=excluded.missing_facts_json AND
                        action_queue_items.prohibited_claims_json=excluded.prohibited_claims_json AND
                        action_queue_items.draft_json=excluded.draft_json
                    THEN action_queue_items.human_confirmation_state ELSE 'pending' END,
                    updated_at=excluded.updated_at
                """,
                (
                    action_id,
                    source.run_id,
                    current_features[customer_key]["feature_snapshot_id"],
                    customer_key,
                    source.profile_id,
                    queue["queue_date"],
                    lane,
                    int(item["priority_score"]),
                    str(item["priority_version"]),
                    candidate.phone_hmac,
                    json_dumps(item["reason_codes"]),
                    json_dumps(item["contact_window"]),
                    str(item["recommended_action"]),
                    str(queue["policy_version"]),
                    json_dumps(item["signals"]),
                    json_dumps(item["required_facts"]),
                    json_dumps(item["missing_facts"]),
                    json_dumps(item["prohibited_claims"]),
                    json_dumps(item["draft"]),
                    confidence,
                    json_dumps(item["freshness"]),
                    created_at,
                    created_at,
                ),
            )


def _build_profile_artifacts(
    connection,
    source: _ProfileSource,
    *,
    as_of_at: datetime,
    collector_status: str,
    secret: str,
    order_source: Optional[_OrderSource],
) -> Dict[str, object]:
    run = connection.execute(
        "SELECT hmac_key_fingerprint FROM pipeline_runs WHERE run_id=?",
        (source.run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError("profile source references a missing pipeline run")
    if str(run["hmac_key_fingerprint"]) != hmac_key_fingerprint(secret):
        raise RuntimeError("HMAC key fingerprint mismatch")

    messages = _load_messages(connection, source, as_of_at=as_of_at)
    identity_rows = _load_identity_rows(connection, source.profile_id)
    outcome_links = _load_outcome_links(connection, source.profile_id)
    valid_order_source = bool(
        order_source is not None
        and order_source.run_id == source.run_id
        and order_source.synced_at <= as_of_at
    )
    point_in_time_orders = _truncate_orders(
        order_source.rows if valid_order_source and order_source else (), as_of_at
    )
    feature_input_index = _index_feature_inputs(
        identity_rows, point_in_time_orders, messages
    )

    actual_status = str(collector_status or "").strip().lower()
    historical_snapshot = bool(
        source.profile_observed_until is not None
        and source.profile_observed_until > as_of_at
    )
    effective_status = (
        "running"
        if actual_status == "running" and source.source_healthy and not historical_snapshot
        else "stopped"
    )
    current_features, _freshness = _feature_rows_at(
        source,
        cutoff=as_of_at,
        target_customers=source.customer_keys,
        identity_rows=identity_rows,
        orders=point_in_time_orders,
        messages=messages,
        order_source=order_source if valid_order_source else None,
        effective_collector_status=effective_status,
        secret=secret,
        input_index=feature_input_index,
    )

    card_sources = {
        customer_key: CardSource(
            profile_id=source.profile_id,
            source_snapshot_id=source.snapshot_by_customer[customer_key],
            observation_until=(
                min(observed, as_of_at) if observed is not None else None
            ),
        )
        for customer_key, observed in source.observed_by_customer.items()
    }
    cards = build_decision_cards(messages, card_sources, secret=secret)

    feature_rows: Dict[str, Dict[str, object]] = {
        str(row["feature_snapshot_id"]): dict(row)
        for row in current_features.values()
    }
    card_feature_ids: Dict[str, str] = {}
    feature_cache: Dict[Tuple[str, str], Dict[str, object]] = {}
    for card in cards:
        card_at = _moment(card.as_of_at, field="card as_of_at")
        cache_key = (card.customer_key, _iso(card_at))
        feature_row = feature_cache.get(cache_key)
        if feature_row is None:
            historical_rows, _ = _feature_rows_at(
                source,
                cutoff=card_at,
                target_customers=(card.customer_key,),
                identity_rows=identity_rows,
                orders=point_in_time_orders,
                messages=messages,
                order_source=order_source if valid_order_source else None,
                effective_collector_status=effective_status,
                secret=secret,
                input_index=feature_input_index,
            )
            feature_row = historical_rows[card.customer_key]
            feature_cache[cache_key] = feature_row
        feature_rows[str(feature_row["feature_snapshot_id"])] = feature_row
        card_feature_ids[card.card_id] = str(feature_row["feature_snapshot_id"])

    if valid_order_source and order_source is not None:
        outcomes = attach_outcomes(
            cards,
            outcome_links,
            point_in_time_orders,
            orders_observed_until=order_source.synced_at,
            computed_at=as_of_at,
        )
    else:
        outcomes = _unknown_outcomes(cards, outcome_links, computed_at=as_of_at)

    candidates = _candidate_rows(
        connection,
        source,
        as_of_at=as_of_at,
        current_features=current_features,
        identity_rows=identity_rows,
        messages=messages,
        orders=point_in_time_orders,
        secret=secret,
    )
    message_snapshot_at = source.profile_observed_until
    if message_snapshot_at is None:
        message_snapshot_at = as_of_at - timedelta(days=36500)
    elif message_snapshot_at > as_of_at:
        message_snapshot_at = as_of_at
    queue_context = QueueContext(
        profile_id=source.profile_id,
        queue_date=as_of_at.date(),
        as_of_at=as_of_at,
        message_snapshot_at=message_snapshot_at,
        message_status=effective_status,
        order_snapshot_at=(
            order_source.synced_at if valid_order_source and order_source else None
        ),
    )
    queue = build_action_queue(queue_context, candidates)

    # The point-in-time cutoff is also the deterministic artifact timestamp.
    # Re-running an identical profile/cutoff therefore does not manufacture a
    # new logical version or rewrite ``updated_at`` with wall-clock time.
    created_at = _iso(as_of_at)
    with connection:
        for row in feature_rows.values():
            _persist_feature(connection, row, created_at)
        for card in cards:
            _persist_card(connection, card, created_at)
        for card in cards:
            connection.execute(
                """
                INSERT INTO card_feature_snapshots(
                    card_id,feature_snapshot_id,feature_payload_json,created_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(card_id) DO UPDATE SET
                    feature_snapshot_id=excluded.feature_snapshot_id,
                    feature_payload_json=excluded.feature_payload_json
                """,
                (
                    card.card_id,
                    card_feature_ids[card.card_id],
                    json_dumps(feature_rows[card_feature_ids[card.card_id]]["profile"]),
                    created_at,
                ),
            )
            annotation = _audit_annotation(card)
            connection.execute(
                """
                INSERT INTO action_annotations(
                    card_id,customer_signal,reply_strategy,reuse_status,
                    required_facts_json,prohibited_claims_json,annotation_json,
                    rule_version,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(card_id) DO UPDATE SET
                    customer_signal=excluded.customer_signal,
                    reply_strategy=excluded.reply_strategy,
                    reuse_status=excluded.reuse_status,
                    required_facts_json=excluded.required_facts_json,
                    prohibited_claims_json=excluded.prohibited_claims_json,
                    annotation_json=excluded.annotation_json,
                    rule_version=excluded.rule_version
                """,
                (
                    card.card_id,
                    annotation["customer_signal"],
                    annotation["reply_strategy"],
                    annotation["reuse_status"],
                    json_dumps(annotation["required_facts"]),
                    json_dumps(annotation["prohibited_claims"]),
                    json_dumps(annotation["annotation"]),
                    ANNOTATION_RULE_VERSION,
                    created_at,
                ),
            )
        for outcome in outcomes.values():
            _persist_outcome(connection, outcome)
        _persist_queue(
            connection,
            source,
            queue,
            candidates,
            current_features,
            created_at=created_at,
            secret=secret,
        )

    return {
        "run_id": source.run_id,
        "feature_snapshots": len(feature_rows),
        "decision_cards": len(cards),
        "outcomes": len(outcomes),
        "queue_status": queue["status"],
        "queue_counts": dict(queue["counts"]),
        "policy_version": POLICY_VERSION,
        "send_allowed": False,
    }


def build_action_artifacts(
    db_path: Path,
    *,
    as_of_at: object,
    collector_status: str,
    profile_id: Optional[str] = None,
    secret: Optional[str] = None,
) -> Dict[str, object]:
    """Build and persist deterministic review-only action artifacts.

    The returned object contains aggregate counts and opaque profile/run handles
    only.  Phone HMACs remain inside SQLite solely for de-duplication and never
    enter the return value.
    """

    as_of = _moment(as_of_at, field="as_of_at")
    normalized_status = str(collector_status or "").strip().lower()
    if not normalized_status:
        raise ValueError("collector_status is required")
    actual_secret = secret or os.environ.get(
        "WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET
    )
    database = Path(db_path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError("action pipeline requires an initialized database")

    connection = open_store(str(database))
    try:
        # M0 run databases are already versioned.  Upgrade an older database
        # once, but avoid touching build metadata on every idempotent rebuild.
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version < 4 or not _action_schema_complete(connection):
            initialize_schema(connection)
        quality_before = {
            str(row["run_id"]): str(row["quality_json"])
            for row in connection.execute("SELECT run_id,quality_json FROM pipeline_runs")
        }
        sources = _load_profile_sources(connection, profile_id)
        order_source = _load_order_source(connection)
        profiles: Dict[str, object] = {}
        for source in sources:
            profiles[source.profile_id] = _build_profile_artifacts(
                connection,
                source,
                as_of_at=as_of,
                collector_status=normalized_status,
                secret=actual_secret,
                order_source=order_source,
            )
        quality_after = {
            str(row["run_id"]): str(row["quality_json"])
            for row in connection.execute("SELECT run_id,quality_json FROM pipeline_runs")
        }
        if quality_after != quality_before:
            raise RuntimeError("action pipeline must not change M0 acceptance metadata")
        return {
            "as_of_at": _iso(as_of),
            "collector_status": normalized_status,
            "pipeline_version": PIPELINE_VERSION,
            "profiles": profiles,
            "automatic_send": False,
        }
    finally:
        connection.close()


__all__ = ["PIPELINE_VERSION", "build_action_artifacts"]
