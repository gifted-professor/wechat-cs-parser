"""Privacy-safe, deterministic rules for the daily customer action queue.

This module deliberately accepts only opaque identifiers and derived signals.
It does not read source systems, call a model, or expose the phone HMAC used for
same-day de-duplication.  The generated draft is the safe fallback when a Kimi
rewrite is unavailable or rejected by downstream grounding checks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


POLICY_VERSION = "action-queue-rules-v2"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MESSAGE_MAX_AGE = timedelta(minutes=15)
ORDER_MAX_AGE = timedelta(hours=24)
PROACTIVE_COOLDOWN = timedelta(days=7)
AOLAI1_DAILY_PROACTIVE_LIMIT = 20
MIN_PERSONAL_HOUR_OBSERVATIONS = 3
CONSECUTIVE_NO_REPLY_LIMIT = 2

LANES = ("reply_now", "proactive_today", "suppressed")
INTENT_SIGNALS = frozenset({"positive", "negative", "mixed", "unknown"})
ALLOWED_FACT_CODES = frozenset(
    {
        "aftersales_policy",
        "current_price",
        "customer_request",
        "delivery_estimate",
        "discount",
        "inventory",
        "order_status",
        "policy",
        "product_category",
        "product_code",
        "promotion",
        "size_availability",
    }
)
PROHIBITED_CLAIMS = (
    "unverified_price",
    "unverified_inventory",
    "unverified_size",
    "unverified_discount",
    "unverified_policy",
    "guaranteed_delivery",
    "guaranteed_outcome",
)

_OPAQUE_CUSTOMER = re.compile(r"^customer_[0-9a-f]{16,64}$")
_OPAQUE_PHONE = re.compile(r"^phone_[0-9a-f]{16,64}$")
_PROFILE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_HEALTHY_MESSAGE_STATUSES = frozenset({"running"})
_INTENT_RANK = {"positive": 3, "mixed": 2, "unknown": 1, "negative": 0}
_SUPPRESSION_RANK = {
    "message_collection_unhealthy": 0,
    "message_snapshot_stale": 1,
    "identity_conflict": 2,
    "aftersales_open": 3,
    "explicit_rejection": 4,
    "manual_suppression": 5,
    "recently_ordered": 6,
    "consecutive_no_reply": 7,
    "facts_insufficient": 8,
    "required_facts_missing": 9,
    "phone_identity_missing": 10,
    "proactive_cooldown": 11,
    "order_snapshot_stale_for_proactive": 12,
    "duplicate_phone_today": 13,
    "daily_proactive_limit": 14,
    "not_actionable_today": 15,
}


@dataclass(frozen=True)
class QueueContext:
    """Point-in-time health and policy inputs for one profile and local date."""

    profile_id: str
    queue_date: date
    as_of_at: datetime
    message_snapshot_at: datetime
    message_status: str
    order_snapshot_at: Optional[datetime]
    proactive_limit: Optional[int] = None


@dataclass(frozen=True)
class ActionCandidate:
    """An anonymous candidate containing only rule-safe derived attributes."""

    customer_key: str
    profile_id: str
    phone_hmac: Optional[str] = None
    unresolved_inbound: bool = False
    proactive_eligible: bool = False
    promised_followup_at: Optional[datetime] = None
    value_score: Optional[float] = None
    repurchase_score: Optional[float] = None
    intent_signal: str = "unknown"
    product_candidate_score: Optional[float] = None
    aftersales_open: bool = False
    explicit_rejection: bool = False
    manual_suppression: bool = False
    recently_ordered: bool = False
    consecutive_no_reply: int = 0
    identity_conflict: bool = False
    facts_sufficient: bool = False
    required_fact_codes: Tuple[str, ...] = ()
    available_fact_codes: Tuple[str, ...] = ()
    last_proactive_at: Optional[datetime] = None
    preferred_contact_hour: Optional[int] = None
    active_hour_observations: int = 0


@dataclass
class _Decision:
    candidate: ActionCandidate
    lane: str
    reason_codes: List[str]
    promise_priority: bool
    order_fresh: bool


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s must be timezone-aware" % field)
    return value


def _age(as_of_at: datetime, snapshot_at: datetime, field: str) -> timedelta:
    age = as_of_at - snapshot_at
    if age < timedelta(0):
        raise ValueError("%s cannot be after as_of_at" % field)
    return age


def _validate_context(context: QueueContext) -> Tuple[timedelta, Optional[timedelta]]:
    if not _PROFILE.fullmatch(context.profile_id or ""):
        raise ValueError("profile_id must be a safe local profile identifier")
    if not isinstance(context.queue_date, date) or isinstance(context.queue_date, datetime):
        raise ValueError("queue_date must be a date")
    as_of_at = _aware(context.as_of_at, "as_of_at")
    if as_of_at.astimezone(SHANGHAI).date() != context.queue_date:
        raise ValueError("queue_date must match as_of_at in Asia/Shanghai")
    message_age = _age(
        as_of_at,
        _aware(context.message_snapshot_at, "message_snapshot_at"),
        "message_snapshot_at",
    )
    order_age = None
    if context.order_snapshot_at is not None:
        order_age = _age(
            as_of_at,
            _aware(context.order_snapshot_at, "order_snapshot_at"),
            "order_snapshot_at",
        )
    if context.proactive_limit is not None and context.proactive_limit < 0:
        raise ValueError("proactive_limit cannot be negative")
    return message_age, order_age


def _validate_score(value: Optional[float], field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError("%s must be between 0 and 100" % field)


def _validate_fact_codes(values: Sequence[str], field: str) -> None:
    if isinstance(values, (str, bytes)):
        raise ValueError("%s must contain fact codes, not text" % field)
    if len(set(values)) != len(values):
        raise ValueError("%s cannot contain duplicate fact codes" % field)
    unknown = sorted(set(values) - ALLOWED_FACT_CODES)
    if unknown:
        raise ValueError("%s contains non-whitelisted fact codes" % field)


def _validate_candidate(candidate: ActionCandidate, context: QueueContext) -> None:
    if not _OPAQUE_CUSTOMER.fullmatch(candidate.customer_key or ""):
        raise ValueError("customer_key must be an opaque customer_key")
    if candidate.phone_hmac is not None and not _OPAQUE_PHONE.fullmatch(candidate.phone_hmac):
        raise ValueError("phone_hmac must be an opaque phone HMAC")
    if candidate.profile_id != context.profile_id:
        raise ValueError("candidate profile_id must match queue profile_id")
    if candidate.intent_signal not in INTENT_SIGNALS:
        raise ValueError("intent_signal is invalid")
    _validate_score(candidate.value_score, "value_score")
    _validate_score(candidate.repurchase_score, "repurchase_score")
    _validate_score(candidate.product_candidate_score, "product_candidate_score")
    if candidate.consecutive_no_reply < 0:
        raise ValueError("consecutive_no_reply cannot be negative")
    if candidate.active_hour_observations < 0:
        raise ValueError("active_hour_observations cannot be negative")
    if (
        candidate.preferred_contact_hour is not None
        and not 0 <= candidate.preferred_contact_hour <= 23
    ):
        raise ValueError("preferred_contact_hour must be between 0 and 23")
    _validate_fact_codes(candidate.required_fact_codes, "required_fact_codes")
    _validate_fact_codes(candidate.available_fact_codes, "available_fact_codes")
    for field, value in (
        ("promised_followup_at", candidate.promised_followup_at),
        ("last_proactive_at", candidate.last_proactive_at),
    ):
        if value is not None:
            _aware(value, field)
    if candidate.last_proactive_at is not None and candidate.last_proactive_at > context.as_of_at:
        raise ValueError("last_proactive_at cannot be after as_of_at")


def _freshness(
    context: QueueContext,
    message_age: timedelta,
    order_age: Optional[timedelta],
) -> Dict[str, Dict[str, object]]:
    message_status = str(context.message_status or "").strip().lower()
    message_state = "fresh"
    if message_status not in _HEALTHY_MESSAGE_STATUSES:
        message_state = "unhealthy"
    elif message_age > MESSAGE_MAX_AGE:
        message_state = "stale"
    if order_age is None:
        order_state = "missing"
    elif order_age > ORDER_MAX_AGE:
        order_state = "stale"
    else:
        order_state = "fresh"
    return {
        "messages": {
            "state": message_state,
            "age_seconds": int(message_age.total_seconds()),
            "max_age_seconds": int(MESSAGE_MAX_AGE.total_seconds()),
            "snapshot_at": context.message_snapshot_at.isoformat(),
        },
        "orders": {
            "state": order_state,
            "age_seconds": int(order_age.total_seconds()) if order_age is not None else None,
            "max_age_seconds": int(ORDER_MAX_AGE.total_seconds()),
            "snapshot_at": (
                context.order_snapshot_at.isoformat()
                if context.order_snapshot_at is not None
                else None
            ),
        },
    }


def _promise_priority(candidate: ActionCandidate, context: QueueContext) -> bool:
    if candidate.promised_followup_at is None:
        return False
    local_date = candidate.promised_followup_at.astimezone(SHANGHAI).date()
    return local_date <= context.queue_date


def _missing_facts(candidate: ActionCandidate) -> List[str]:
    available = set(candidate.available_fact_codes)
    return [code for code in candidate.required_fact_codes if code not in available]


def _hard_suppression(candidate: ActionCandidate) -> List[str]:
    reasons: List[str] = []
    if candidate.identity_conflict:
        reasons.append("identity_conflict")
    if candidate.aftersales_open:
        reasons.append("aftersales_open")
    if candidate.explicit_rejection:
        reasons.append("explicit_rejection")
    if candidate.manual_suppression:
        reasons.append("manual_suppression")
    if candidate.recently_ordered:
        reasons.append("recently_ordered")
    if candidate.consecutive_no_reply >= CONSECUTIVE_NO_REPLY_LIMIT:
        reasons.append("consecutive_no_reply")
    if not candidate.facts_sufficient:
        reasons.append("facts_insufficient")
    if _missing_facts(candidate):
        reasons.append("required_facts_missing")
    return reasons


def _rank(decision: _Decision) -> Tuple[float, ...]:
    candidate = decision.candidate
    value = (
        candidate.value_score
        if decision.order_fresh and candidate.value_score is not None
        else 0
    )
    repurchase = (
        candidate.repurchase_score
        if decision.order_fresh and candidate.repurchase_score is not None
        else 0
    )
    product = (
        candidate.product_candidate_score
        if decision.order_fresh and candidate.product_candidate_score is not None
        else 0
    )
    return (
        1 if candidate.unresolved_inbound else 0,
        1 if decision.promise_priority else 0,
        float(value),
        float(repurchase),
        float(_INTENT_RANK[candidate.intent_signal]),
        float(product),
    )


def _sort_key(decision: _Decision) -> Tuple[object, ...]:
    rank = _rank(decision)
    return tuple(-part for part in rank) + (decision.candidate.customer_key,)


def _priority_score(decision: _Decision) -> int:
    if decision.lane == "suppressed":
        return 0
    candidate = decision.candidate
    value = (
        candidate.value_score
        if decision.order_fresh and candidate.value_score is not None
        else 0
    )
    repurchase = (
        candidate.repurchase_score
        if decision.order_fresh and candidate.repurchase_score is not None
        else 0
    )
    product = (
        candidate.product_candidate_score
        if decision.order_fresh and candidate.product_candidate_score is not None
        else 0
    )
    return (
        (50 if candidate.unresolved_inbound else 0)
        + (20 if decision.promise_priority else 0)
        + round(float(value) * 0.08)
        + round(float(repurchase) * 0.06)
        + _INTENT_RANK[candidate.intent_signal]
        + round(float(product) * 0.02)
    )


def _recommended_action(decision: _Decision) -> str:
    if decision.lane == "reply_now":
        return "reply_to_inbound"
    if decision.lane == "proactive_today":
        return (
            "follow_up_as_promised"
            if decision.promise_priority
            else "proactive_followup"
        )
    reasons = set(decision.reason_codes)
    if "message_collection_unhealthy" in reasons:
        return "restore_message_collection"
    if "message_snapshot_stale" in reasons:
        return "refresh_message_snapshot"
    if "identity_conflict" in reasons or "phone_identity_missing" in reasons:
        return "resolve_identity"
    if "aftersales_open" in reasons:
        return "route_to_human_aftersales"
    if "facts_insufficient" in reasons or "required_facts_missing" in reasons:
        return "verify_facts"
    return "do_not_contact"


def _contact_window(decision: _Decision, context: QueueContext) -> Dict[str, object]:
    candidate = decision.candidate
    if decision.lane == "suppressed":
        return {"mode": "none", "timezone": "Asia/Shanghai"}
    if decision.lane == "reply_now":
        return {"mode": "as_soon_as_possible", "timezone": "Asia/Shanghai"}
    if decision.promise_priority and candidate.promised_followup_at is not None:
        promised = candidate.promised_followup_at.astimezone(SHANGHAI)
        if promised > context.as_of_at.astimezone(SHANGHAI):
            return {
                "mode": "promised_time",
                "timezone": "Asia/Shanghai",
                "start_hour": promised.hour,
                "end_hour": (promised.hour + 1) % 24,
            }
        return {"mode": "as_soon_as_possible", "timezone": "Asia/Shanghai"}
    if (
        candidate.preferred_contact_hour is not None
        and candidate.active_hour_observations >= MIN_PERSONAL_HOUR_OBSERVATIONS
    ):
        return {
            "mode": "personal_history",
            "timezone": "Asia/Shanghai",
            "start_hour": candidate.preferred_contact_hour,
            "end_hour": (candidate.preferred_contact_hour + 1) % 24,
        }
    return {
        "mode": "work_hours_manual_choice",
        "timezone": "Asia/Shanghai",
        "start_hour": 9,
        "end_hour": 18,
    }


def _rule_draft(decision: _Decision) -> Dict[str, object]:
    if decision.lane == "reply_now":
        text = (
            "收到您的消息。我先核对相关事实，确认后给您准确回复；"
            "核实前不对价格、库存、尺码、优惠、政策或时效作承诺。"
        )
    elif decision.lane == "proactive_today" and decision.promise_priority:
        text = (
            "按之前约定来跟进您关注的事项。我先核对相关事实，"
            "确认后再给您准确建议。"
        )
    elif decision.lane == "proactive_today":
        text = (
            "想跟进您之前关注的需求。如果仍有需要，我可以先核对相关事实，"
            "再给您合适的建议。"
        )
    else:
        text = "【不发送】当前动作已被规则暂停，请先完成人工检查。"
    return {
        "mode": "rule_skeleton",
        "text": text,
        "model_used": False,
        "fallback_for": "kimi_unavailable_or_rejected",
    }


def _confidence(decision: _Decision) -> str:
    if decision.lane == "suppressed" or decision.candidate.unresolved_inbound:
        return "high"
    if decision.promise_priority:
        return "high"
    return "medium"


def _reason_codes(decision: _Decision) -> List[str]:
    if decision.lane == "suppressed":
        return sorted(
            set(decision.reason_codes),
            key=lambda code: (_SUPPRESSION_RANK.get(code, 999), code),
        )
    candidate = decision.candidate
    reasons: List[str] = []
    if candidate.unresolved_inbound:
        reasons.append("unanswered_inbound")
    if decision.promise_priority:
        reasons.append("promised_followup")
    if decision.order_fresh and candidate.value_score:
        reasons.append("customer_value_signal")
    if decision.order_fresh and candidate.repurchase_score:
        reasons.append("repurchase_signal")
    if candidate.intent_signal in {"positive", "mixed"}:
        reasons.append("%s_intent" % candidate.intent_signal)
    if decision.order_fresh and candidate.product_candidate_score:
        reasons.append("product_candidate_signal")
    if not reasons:
        reasons.append("proactive_eligible")
    return reasons


def _action_id(context: QueueContext, decision: _Decision) -> str:
    payload = "\x1f".join(
        (
            POLICY_VERSION,
            context.profile_id,
            context.queue_date.isoformat(),
            decision.candidate.customer_key,
            decision.lane,
        )
    )
    return "action_%s" % hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _signals(candidate: ActionCandidate, order_fresh: bool) -> Dict[str, object]:
    return {
        "value_score": candidate.value_score if order_fresh else None,
        "repurchase_score": candidate.repurchase_score if order_fresh else None,
        "intent_signal": candidate.intent_signal,
        "product_candidate_score": candidate.product_candidate_score if order_fresh else None,
    }


def _item(
    decision: _Decision,
    context: QueueContext,
    freshness: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    candidate = decision.candidate
    message_state = str(freshness["messages"].get("state") or "unknown")
    realtime_reply_available = message_state == "fresh"
    historical_proactive = (
        decision.lane == "proactive_today" and not realtime_reply_available
    )
    reason_codes = _reason_codes(decision)
    if historical_proactive:
        reason_codes.extend(("historical_snapshot_only", "contact_precheck_required"))
    return {
        "action_id": _action_id(context, decision),
        "customer_key": candidate.customer_key,
        "lane": decision.lane,
        "priority_score": _priority_score(decision),
        "priority_version": POLICY_VERSION,
        "reason_codes": reason_codes,
        "recommended_action": _recommended_action(decision),
        "contact_window": _contact_window(decision, context),
        "signals": _signals(candidate, decision.order_fresh),
        "required_facts": list(candidate.required_fact_codes),
        "missing_facts": _missing_facts(candidate),
        "prohibited_claims": list(PROHIBITED_CLAIMS),
        "draft": _rule_draft(decision),
        "confidence": _confidence(decision),
        "freshness": {
            "messages": dict(freshness["messages"]),
            "orders": dict(freshness["orders"]),
        },
        "data_mode": (
            "historical_snapshot" if historical_proactive else "current_snapshot"
        ),
        "snapshot_cutoff": freshness["messages"].get("snapshot_at"),
        "realtime_reply_available": realtime_reply_available,
        "contact_precheck_required": historical_proactive,
        "human_confirmation_required": True,
        "send_allowed": False,
    }


def _initial_decision(
    candidate: ActionCandidate,
    context: QueueContext,
    order_fresh: bool,
    reply_lane_reasons: Sequence[str],
) -> _Decision:
    promise_priority = _promise_priority(candidate, context)
    hard_reasons = _hard_suppression(candidate)
    if hard_reasons:
        return _Decision(candidate, "suppressed", hard_reasons, promise_priority, order_fresh)
    if candidate.unresolved_inbound:
        if reply_lane_reasons:
            return _Decision(
                candidate,
                "suppressed",
                list(reply_lane_reasons),
                promise_priority,
                order_fresh,
            )
        return _Decision(candidate, "reply_now", [], promise_priority, order_fresh)
    wants_proactive = candidate.proactive_eligible or promise_priority
    if wants_proactive:
        reasons: List[str] = []
        if candidate.phone_hmac is None:
            reasons.append("phone_identity_missing")
        if (
            candidate.last_proactive_at is not None
            and context.as_of_at - candidate.last_proactive_at < PROACTIVE_COOLDOWN
        ):
            reasons.append("proactive_cooldown")
        if not order_fresh and not promise_priority:
            reasons.append("order_snapshot_stale_for_proactive")
        if reasons:
            return _Decision(candidate, "suppressed", reasons, promise_priority, order_fresh)
        return _Decision(candidate, "proactive_today", [], promise_priority, order_fresh)
    return _Decision(
        candidate,
        "suppressed",
        ["not_actionable_today"],
        promise_priority,
        order_fresh,
    )


def _apply_proactive_guards(
    decisions: Iterable[_Decision], context: QueueContext
) -> List[_Decision]:
    all_decisions = list(decisions)
    proactive = sorted(
        (decision for decision in all_decisions if decision.lane == "proactive_today"),
        key=_sort_key,
    )
    seen_phones = set()
    accepted: List[_Decision] = []
    for decision in proactive:
        phone_hmac = decision.candidate.phone_hmac
        if phone_hmac in seen_phones:
            decision.lane = "suppressed"
            decision.reason_codes = ["duplicate_phone_today"]
            continue
        seen_phones.add(phone_hmac)
        accepted.append(decision)

    default_limit = (
        AOLAI1_DAILY_PROACTIVE_LIMIT if context.profile_id == "aolai1" else None
    )
    limit = context.proactive_limit if context.proactive_limit is not None else default_limit
    if limit is not None:
        for decision in accepted[limit:]:
            decision.lane = "suppressed"
            decision.reason_codes = ["daily_proactive_limit"]
    return all_decisions


def build_action_queue(
    context: QueueContext, candidates: Iterable[ActionCandidate]
) -> Dict[str, object]:
    """Build a deterministic, review-only three-lane action queue.

    Message health is fail-closed only for the real-time ``reply_now`` lane.
    A same-day historical snapshot may still produce review-only proactive
    candidates when order facts remain fresh; every such item is marked for a
    manual pre-contact check.  No phone HMAC or source identifier is returned.
    """

    message_age, order_age = _validate_context(context)
    candidate_list = list(candidates)
    seen_customers = set()
    for candidate in candidate_list:
        if not isinstance(candidate, ActionCandidate):
            raise ValueError("candidates must contain ActionCandidate values")
        _validate_candidate(candidate, context)
        if candidate.customer_key in seen_customers:
            raise ValueError("customer_key cannot appear more than once per queue")
        seen_customers.add(candidate.customer_key)

    freshness = _freshness(context, message_age, order_age)
    reply_lane_reasons: List[str] = []
    if freshness["messages"]["state"] == "unhealthy":
        reply_lane_reasons.append("message_collection_unhealthy")
    elif freshness["messages"]["state"] == "stale":
        reply_lane_reasons.append("message_snapshot_stale")
    order_fresh = freshness["orders"]["state"] == "fresh"

    decisions = [
        _initial_decision(candidate, context, order_fresh, reply_lane_reasons)
        for candidate in candidate_list
    ]
    decisions = _apply_proactive_guards(decisions, context)

    grouped: Dict[str, List[_Decision]] = {lane: [] for lane in LANES}
    for decision in decisions:
        grouped[decision.lane].append(decision)
    for lane in ("reply_now", "proactive_today"):
        grouped[lane].sort(key=_sort_key)
    grouped["suppressed"].sort(
        key=lambda decision: (
            min((_SUPPRESSION_RANK.get(code, 999) for code in decision.reason_codes), default=999),
            decision.candidate.customer_key,
        )
    )

    lanes = {
        lane: [_item(decision, context, freshness) for decision in grouped[lane]]
        for lane in LANES
    }
    # Message freshness is now a lane restriction rather than a global queue
    # failure.  Keep the legacy persisted status values: a queue with safe
    # proactive candidates is ready, while an entirely non-actionable stale
    # queue remains blocked.
    block_reasons = (
        list(reply_lane_reasons)
        if reply_lane_reasons and not lanes["proactive_today"]
        else []
    )
    if block_reasons:
        status = "blocked"
    elif not order_fresh:
        status = "degraded_order_data"
    else:
        status = "ready"
    return {
        "profile_id": context.profile_id,
        "queue_date": context.queue_date.isoformat(),
        "generated_at": context.as_of_at.isoformat(),
        "policy_version": POLICY_VERSION,
        "status": status,
        "block_reasons": block_reasons,
        "lane_restrictions": {
            "reply_now": list(reply_lane_reasons),
            "proactive_today": [],
        },
        "freshness": freshness,
        "lanes": lanes,
        "counts": {lane: len(lanes[lane]) for lane in LANES},
    }


__all__ = [
    "ALLOWED_FACT_CODES",
    "AOLAI1_DAILY_PROACTIVE_LIMIT",
    "ActionCandidate",
    "LANES",
    "POLICY_VERSION",
    "PROHIBITED_CLAIMS",
    "QueueContext",
    "build_action_queue",
]
