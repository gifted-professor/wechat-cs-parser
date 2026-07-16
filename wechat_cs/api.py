"""Small, local-only HTTP API for the WeChat customer-service workbench.

The server deliberately uses only the Python standard library.  It reads the
derived SQLite database, never the original WeChat export, and it never logs
request bodies or response bodies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .core import redact_text
from .knowledge import KnowledgeRegistry
from .store import get_health as get_store_health

try:
    from zoneinfo import ZoneInfo

    SHANGHAI = ZoneInfo("Asia/Shanghai")
except (ImportError, Exception):  # pragma: no cover - Python/OS fallback
    SHANGHAI = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / ".wechat-cs" / "data" / "wechat_cs.sqlite3"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STALE_AFTER_DAYS = 14

SAFE_REVIEW_STATUSES = {"pending", "approved", "rejected"}
SAFE_FEEDBACK_OUTCOMES = {"adopted", "accepted", "edited", "rejected"}
SAFE_ACTION_FEEDBACK_OUTCOMES = {"adopted", "edited", "rejected"}
ACTION_QUEUE_LANES = ("reply_now", "proactive_today", "suppressed")
DEFAULT_ACTION_QUEUE_LIMIT = 20
MAX_ACTION_QUEUE_LIMIT = 20
ACTION_MESSAGE_MAX_AGE_SECONDS = 15 * 60
ACTION_ORDER_MAX_AGE_SECONDS = 24 * 60 * 60
ACTION_REPLY_LANE_REASONS = frozenset(
    {"message_collection_unhealthy", "message_snapshot_stale"}
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:phone|mobile|wxid|wechat(?:_?id)?|raw_(?:wechat_)?id|conversation_(?:id|username)|"
    r"customer_name|display_name|person_name|recipient|contact_name|address|username|avatar|headimg|"
    r"source_(?:file|id|snapshot_id)|record_id|order_line_id|tracking|"
    r"hmac(?:_?key)?|secret|api_?key)",
    re.IGNORECASE,
)
DYNAMIC_FACT_RE = re.compile(
    r"价格|价钱|多少钱|库存|现货|缺货|到货|发货|物流|快递|运单|退款|退货|补发|赔付|赔偿|优惠|折扣|price|stock|refund|shipping",
    re.IGNORECASE,
)

PRIVATE_REDACTION_FLAGS = frozenset(
    {
        "address",
        "phone",
        "person_name",
        "id_card",
        "email",
        "url",
        "wechat_id",
        "tracking_or_order_id",
    }
)
ACTION_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
ACTION_FACT_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
OPAQUE_ACTION_CUSTOMER_RE = re.compile(r"^customer_[0-9a-f]{16,64}$")
OPAQUE_ACTION_ID_RE = re.compile(r"^action_[0-9a-f]{16,64}$")
ACTION_ALLOWED_FACT_CODES = frozenset(
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
MODEL_FORBIDDEN_KEY_RE = re.compile(
    r"(?:observed_?action|outcomes?|paid_(?:1|3|7|30)d|retained_30d|aftersale_30d|"
    r"exchange_30d|compensation_30d|matched_orders?|attribution|customer_name|display_name|"
    r"person_name|recipient|contact_name|address|tracking|order_id|raw_id)",
    re.IGNORECASE,
)
GUARANTEE_CLAIM_RE = re.compile(
    r"(?:保证|肯定|一定|绝对|百分之百|100%|无条件|包(?:到|成|过|退))",
    re.IGNORECASE,
)
UNGROUNDED_QUALITATIVE_CLAIM_RE = re.compile(
    r"(?:质量(?:很|非常)?好|品质(?:很|非常)?好|很适合您|特别适合您|强烈推荐|正品保证|效果(?:很|非常)?好)",
    re.IGNORECASE,
)
ACTION_DYNAMIC_CLAIMS: Tuple[Tuple[re.Pattern, frozenset], ...] = (
    (
        re.compile(r"(?:\d+(?:\.\d+)?\s*(?:元|块|rmb)|价格(?:为|是)|售价(?:为|是)|只要\s*\d)", re.IGNORECASE),
        frozenset({"current_price"}),
    ),
    (
        re.compile(r"(?:有现货|现货(?:充足)?|有库存|库存充足|有货|没货|无货|缺货|补货中)"),
        frozenset({"inventory"}),
    ),
    (
        re.compile(r"(?:(?:尺码|码数).{0,12}(?:有|可选|齐全|现货)|\b(?:XS|S|M|L|XL|XXL|XXXL)\s*码)", re.IGNORECASE),
        frozenset({"size_availability", "inventory"}),
    ),
    (
        re.compile(r"(?:(?:优惠|折扣|活动).{0,12}(?:有|为|是|可用|参加|\d)|\d(?:\.\d+)?\s*折)"),
        frozenset({"discount", "promotion"}),
    ),
    (
        re.compile(r"(?:(?:今天|明天|后天|\d+\s*天).{0,12}(?:发货|到货|送达)|预计.{0,12}(?:发货|到货|送达))"),
        frozenset({"delivery_estimate", "order_status"}),
    ),
    (
        re.compile(r"(?:已经?发货|已揽收|正在派送|派送中|已签收|物流.{0,8}(?:更新|显示|到达))"),
        frozenset({"order_status"}),
    ),
    (
        re.compile(r"(?:可以|支持|同意|马上|现在).{0,8}(?:退款|退货|补发|赔付|赔偿)"),
        frozenset({"aftersales_policy", "policy", "order_status"}),
    ),
)


def _now() -> dt.datetime:
    return dt.datetime.now(SHANGHAI)


def _iso_now() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _safe_json(value: Any) -> Any:
    """Recursively drop known raw identifiers from API-visible JSON."""
    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _redact_strings(value: Any) -> Any:
    """Redact sensitive/dynamic string values before external model use."""
    if isinstance(value, Mapping):
        return {
            str(key): _redact_strings(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_redact_strings(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)[0]
    return value


def _action_safe_json(value: Any) -> Any:
    """Remove private keys and redact PII while retaining approved live facts.

    ``redact_text`` also marks volatile business facts such as current prices.
    Those facts are allowed in an action draft after explicit grounding, so the
    original text is retained when no *private* redaction flag was found.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _action_safe_json(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_action_safe_json(item) for item in value]
    if isinstance(value, tuple):
        return [_action_safe_json(item) for item in value]
    if isinstance(value, str):
        redacted, flags = redact_text(value)
        return redacted if PRIVATE_REDACTION_FLAGS.intersection(flags) else value[:4000]
    return value


def _contains_private_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            SENSITIVE_KEY_RE.search(str(key)) is not None or _contains_private_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_private_value(item) for item in value)
    if isinstance(value, str):
        _, flags = redact_text(value)
        return bool(PRIVATE_REDACTION_FLAGS.intersection(flags))
    return False


def _contains_forbidden_model_context(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            MODEL_FORBIDDEN_KEY_RE.search(str(key)) is not None
            or _contains_forbidden_model_context(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_model_context(item) for item in value)
    return False


def _fact_codes(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, str):
            code = item.strip().lower()
        elif isinstance(item, Mapping):
            code = str(item.get("code") or item.get("fact_code") or "").strip().lower()
        else:
            continue
        if (
            ACTION_FACT_CODE_RE.fullmatch(code)
            and code in ACTION_ALLOWED_FACT_CODES
            and code not in result
        ):
            result.append(code)
    return result


def _safe_action_codes(value: Any, *, limit: int = 40) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        code = item.strip().lower()
        if (
            ACTION_FACT_CODE_RE.fullmatch(code)
            and not SENSITIVE_KEY_RE.search(code)
            and not MODEL_FORBIDDEN_KEY_RE.search(code)
            and code not in result
        ):
            result.append(code)
        if len(result) >= limit:
            break
    return result


def _public_action_id(value: Any) -> str:
    action_id = str(value or "")
    safe = str(_action_safe_json(action_id))
    if OPAQUE_ACTION_ID_RE.fullmatch(action_id) or safe == action_id:
        return action_id
    return safe


def _fallback_action_draft(row: Mapping[str, Any], reason: str) -> Dict[str, Any]:
    # Never read the mutable ``draft_json`` here.  That column may contain a
    # prior model draft with facts that are no longer current.  The fallback is
    # deliberately reconstructed from immutable lane/reason codes every time.
    lane = str(row.get("lane") or "suppressed")
    reasons = _safe_action_codes(_parse_json(row.get("reason_codes_json"), []))
    if lane == "reply_now":
        text = (
            "收到您的消息。我先核对相关事实，确认后给您准确回复；"
            "核实前不对价格、库存、尺码、优惠、政策或时效作承诺。"
        )
    elif lane == "proactive_today" and "promised_followup" in reasons:
        text = "按之前约定来跟进您关注的事项。我先核对相关事实，确认后再给您准确建议。"
    elif lane == "proactive_today":
        text = "想跟进您之前关注的需求。如仍有需要，我先核对相关事实，再给您准确建议。"
    else:
        text = "【不发送】当前动作已被规则暂停，请先完成人工检查。"
    safe_text = str(_action_safe_json(text)).strip()
    return {
        "mode": "rule_skeleton",
        "text": safe_text[:4000],
        "model_used": False,
        "model": None,
        "grounding_fact_codes": [],
        "fallback_for": reason,
    }


def _public_action_item(row: Mapping[str, Any]) -> Dict[str, Any]:
    reason_codes = _parse_json(row.get("reason_codes_json"), [])
    contact_window = _parse_json(row.get("contact_window_json"), {})
    required_facts = _fact_codes(_parse_json(row.get("required_facts_json"), []))
    prohibited_claims = _parse_json(row.get("prohibited_claims_json"), [])
    signals = _parse_json(row.get("signals_json"), {})
    missing_facts = _fact_codes(_parse_json(row.get("missing_facts_json"), []))
    draft = _parse_json(row.get("draft_json"), {})
    freshness = _parse_json(row.get("freshness_json"), {})
    freshness = freshness if isinstance(freshness, Mapping) else {}
    message_freshness = freshness.get("messages")
    message_freshness = (
        message_freshness if isinstance(message_freshness, Mapping) else {}
    )
    lane = str(row.get("lane") or "suppressed")
    realtime_reply_available = message_freshness.get("state") == "fresh"
    historical_proactive = lane == "proactive_today" and not realtime_reply_available
    reason_codes = reason_codes if isinstance(reason_codes, list) else []
    if historical_proactive:
        for reason in ("historical_snapshot_only", "contact_precheck_required"):
            if reason not in reason_codes:
                reason_codes.append(reason)
    state = str(row.get("human_confirmation_state") or "pending")
    if state not in SAFE_ACTION_FEEDBACK_OUTCOMES | {"pending"}:
        state = "pending"
    item = {
        "action_id": row.get("action_id"),
        "customer_key": row.get("customer_key"),
        "profile": row.get("profile_id"),
        "date": row.get("queue_date"),
        "lane": lane,
        "priority_score": int(row.get("priority_score") or 0),
        "priority_version": row.get("priority_version"),
        "reason_codes": reason_codes,
        "contact_window": contact_window if isinstance(contact_window, Mapping) else {},
        "recommended_action": row.get("recommended_action"),
        "strategy_version": row.get("strategy_version"),
        "signals": signals if isinstance(signals, Mapping) else {},
        "required_facts": required_facts,
        "missing_facts": missing_facts,
        "prohibited_claims": prohibited_claims if isinstance(prohibited_claims, list) else [],
        "draft": draft if isinstance(draft, Mapping) else {},
        "confidence": row.get("confidence"),
        "freshness": freshness,
        "data_mode": (
            "historical_snapshot" if historical_proactive else "current_snapshot"
        ),
        "snapshot_cutoff": message_freshness.get("snapshot_at"),
        "realtime_reply_available": realtime_reply_available,
        "contact_precheck_required": historical_proactive,
        "human_confirmation_state": state,
        "human_confirmation_required": True,
        # The database schema also fixes this to zero.  Keep the public value
        # hard-coded so a damaged or hand-edited database still cannot imply a
        # send capability.
        "send_allowed": False,
    }
    safe = dict(_action_safe_json(item))
    # Opaque identifiers can contain long digit runs which the general PII
    # redactor intentionally treats like legacy telephone numbers.  Restore
    # only identifiers that pass the strict public-ID grammar; raw source IDs
    # are never selected into ``item`` in the first place.
    action_id = str(item.get("action_id") or "")
    if action_id and len(action_id) <= 160 and re.fullmatch(r"[A-Za-z0-9_.:-]+", action_id):
        safe["action_id"] = _public_action_id(action_id)
    customer_key = str(item.get("customer_key") or "")
    if OPAQUE_ACTION_CUSTOMER_RE.fullmatch(customer_key):
        safe["customer_key"] = customer_key
    elif customer_key:
        # A malformed legacy row must not turn a raw source identifier into a
        # public response.  This branch is intentionally not used for joins.
        safe["customer_key"] = "customer_" + hashlib.sha256(
            customer_key.encode("utf-8")
        ).hexdigest()[:24]
    return safe


def _bigrams(value: str) -> set:
    compact = re.sub(r"\s+", "", (value or "").lower())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _jaccard_similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _runtime_action_freshness(
    value: Mapping[str, Any], generated_at: Any
) -> Tuple[Dict[str, Dict[str, Any]], bool, bool, List[str]]:
    """Advance persisted freshness ages to request time.

    Queue builds are point-in-time artifacts.  A row that was fresh when it was
    built must not remain actionable forever merely because the collector did
    not run again.  The persisted age plus the elapsed wall time reconstructs
    the effective source age and cutoff without reading the raw inbox from the
    API layer.  Callers decide which lane each source can safely support.
    """

    generated = _parse_timestamp(str(generated_at or ""))
    now = _now()
    invalid = generated is None or generated > now + dt.timedelta(seconds=5)
    elapsed = 0.0 if generated is None else max(0.0, (now - generated).total_seconds())
    result: Dict[str, Dict[str, Any]] = {}

    for source, threshold in (
        ("messages", ACTION_MESSAGE_MAX_AGE_SECONDS),
        ("orders", ACTION_ORDER_MAX_AGE_SECONDS),
    ):
        raw = value.get(source)
        if not isinstance(raw, Mapping):
            result[source] = {
                "state": "unknown",
                "age_seconds": None,
                "max_age_seconds": threshold,
            }
            invalid = True
            continue
        state = str(raw.get("state") or "unknown").strip().lower()
        if state not in {"fresh", "stale", "unhealthy", "missing", "unknown"}:
            state = "unknown"
            invalid = True
        raw_age = raw.get("age_seconds")
        age: Optional[float]
        if raw_age is None and state in {"missing", "unknown"}:
            age = None
        elif isinstance(raw_age, bool):
            age = None
            invalid = True
        else:
            try:
                age = float(raw_age)
            except (TypeError, ValueError):
                age = None
                invalid = True
            if age is not None and age < 0:
                age = None
                invalid = True
        effective_age = age + elapsed if age is not None else None
        if state == "fresh" and effective_age is None:
            state = "unknown"
            invalid = True
        elif state == "fresh" and effective_age > threshold:
            state = "stale"
        snapshot_at = _parse_timestamp(str(raw.get("snapshot_at") or ""))
        if snapshot_at is None and generated is not None and age is not None:
            snapshot_at = generated - dt.timedelta(seconds=age)
        if (
            snapshot_at is not None
            and generated is not None
            and snapshot_at > generated + dt.timedelta(seconds=5)
        ):
            snapshot_at = None
            invalid = True
        result[source] = {
            "state": state,
            "age_seconds": int(effective_age) if effective_age is not None else None,
            "max_age_seconds": threshold,
            "snapshot_at": snapshot_at.isoformat() if snapshot_at is not None else None,
        }

    if invalid:
        result["messages"]["state"] = "unknown"
    messages_fresh = result["messages"]["state"] == "fresh" and not invalid
    orders_fresh = result["orders"]["state"] == "fresh" and not invalid
    reasons: List[str] = []
    message_state = result["messages"]["state"]
    if invalid or message_state in {"missing", "unknown"}:
        reasons.append("queue_metadata_invalid")
    elif message_state == "unhealthy":
        reasons.append("message_collection_unhealthy")
    elif message_state == "stale":
        reasons.append("message_snapshot_stale")
    return result, messages_fresh, orders_fresh, reasons


def _hide_runtime_order_signals(
    row: Mapping[str, Any], freshness: Mapping[str, Any]
) -> Dict[str, Any]:
    safe = dict(row)
    signals = _parse_json(safe.get("signals_json"), {})
    signals = dict(signals) if isinstance(signals, Mapping) else {}
    for key in ("value_score", "repurchase_score", "product_candidate_score"):
        signals[key] = None
    safe["signals_json"] = json.dumps(signals, ensure_ascii=False, separators=(",", ":"))
    safe["freshness_json"] = json.dumps(
        freshness, ensure_ascii=False, separators=(",", ":")
    )
    return safe


def _runtime_suppress_action(
    row: Mapping[str, Any], reason: str, freshness: Mapping[str, Any]
) -> Dict[str, Any]:
    safe = _hide_runtime_order_signals(row, freshness)
    reasons = _safe_action_codes(_parse_json(safe.get("reason_codes_json"), []))
    if reason not in reasons:
        reasons.append(reason)
    safe.update(
        {
            "lane": "suppressed",
            "priority_score": 0,
            "reason_codes_json": json.dumps(reasons, separators=(",", ":")),
            "contact_window_json": '{"mode":"none","timezone":"Asia/Shanghai"}',
            "recommended_action": (
                "restore_message_collection"
                if reason == "message_collection_unhealthy"
                else "refresh_message_snapshot"
                if reason in {"message_snapshot_stale", "queue_metadata_invalid"}
                else "verify_facts"
            ),
            "draft_json": json.dumps(
                {
                    "mode": "rule_skeleton",
                    "text": "【不发送】当前动作已被规则暂停，请先完成人工检查。",
                    "model_used": False,
                    "fallback_for": "runtime_fail_closed",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    )
    return safe


def _runtime_guard_action_row(
    row: Mapping[str, Any], run: Mapping[str, Any]
) -> Dict[str, Any]:
    freshness_value = _parse_json(run.get("freshness_json"), {})
    if not isinstance(freshness_value, Mapping):
        freshness_value = {}
    freshness, messages_fresh, orders_fresh, runtime_reasons = (
        _runtime_action_freshness(freshness_value, run.get("as_of_at"))
    )
    guarded = (
        _hide_runtime_order_signals(row, freshness)
        if not orders_fresh
        else dict(row, freshness_json=json.dumps(freshness, separators=(",", ":")))
    )
    persisted_reasons = _safe_action_codes(
        _parse_json(run.get("block_reasons_json"), [])
    )
    run_status = str(run.get("status") or "blocked")
    global_persisted_reasons = [
        reason for reason in persisted_reasons if reason not in ACTION_REPLY_LANE_REASONS
    ]
    global_runtime_reasons = [
        reason for reason in runtime_reasons if reason not in ACTION_REPLY_LANE_REASONS
    ]
    globally_blocked = bool(global_runtime_reasons) or (
        run_status == "blocked"
        and (bool(global_persisted_reasons) or not persisted_reasons)
    )
    lane = str(guarded.get("lane") or "suppressed")
    if globally_blocked:
        reason = (
            global_runtime_reasons[0]
            if global_runtime_reasons
            else global_persisted_reasons[0]
            if global_persisted_reasons
            else "queue_blocked"
        )
        if lane != "suppressed":
            guarded = _runtime_suppress_action(guarded, reason, freshness)
    elif not messages_fresh and lane == "reply_now":
        reason = next(
            (
                item
                for item in runtime_reasons + persisted_reasons
                if item in ACTION_REPLY_LANE_REASONS
            ),
            "queue_metadata_invalid",
        )
        guarded = _runtime_suppress_action(guarded, reason, freshness)
    elif not orders_fresh and lane == "proactive_today":
        reasons = _safe_action_codes(
            _parse_json(guarded.get("reason_codes_json"), [])
        )
        if "promised_followup" not in reasons:
            guarded = _runtime_suppress_action(
                guarded, "order_snapshot_stale_for_proactive", freshness
            )
    return guarded


_ACTION_VERSION_FIELDS = (
    "action_id",
    "run_id",
    "feature_snapshot_id",
    "customer_key",
    "profile_id",
    "queue_date",
    "lane",
    "priority_version",
    "reason_codes_json",
    "contact_window_json",
    "recommended_action",
    "strategy_version",
    "signals_json",
    "required_facts_json",
    "missing_facts_json",
    "prohibited_claims_json",
    "draft_json",
    "human_confirmation_state",
    "updated_at",
)


def _action_version_fingerprint(row: Mapping[str, Any]) -> str:
    material = json.dumps(
        [row.get(field) for field in _ACTION_VERSION_FIELDS],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_loopback(address: str) -> bool:
    host = address.split("%", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", "localhost.localdomain"}


def _request_hostname(value: str) -> str:
    try:
        return str(urlsplit("//" + str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute('PRAGMA table_info("%s")' % table)}


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _public_customer(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "customer_key": row.get("customer_key"),
        "display_name": row.get("display_name") or "未命名客户",
        "last_active_at": row.get("last_active_at"),
        "opportunity_score": int(row.get("opportunity_score") or 0),
        "opportunity_level": row.get("opportunity_level") or "low",
        "aftersales_priority": row.get("aftersales_priority"),
        "summary": row.get("summary") or "",
        "reasons": _safe_json(_parse_json(row.get("reasons_json"), [])),
        "evidence": _safe_json(_parse_json(row.get("evidence_json"), [])),
        "memory": _safe_json(_parse_json(row.get("memory_json"), {})),
        "identity_binding_state": row.get("identity_binding_state") or "unmatched",
        "identity_candidate_count": int(row.get("identity_candidate_count") or 0),
        # An approved phone candidate is not the same as an order-system join.
        # V1 has no remote order source attached to this local database.
        "order_binding_state": "unavailable",
    }


def _public_style_pair(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "pair_id": row.get("pair_id"),
        "customer_key": row.get("customer_key"),
        "trigger_text": row.get("trigger_text") or "",
        "reply_text": row.get("reply_text") or "",
        "context": _safe_json(_parse_json(row.get("context_json"), [])),
        "intent_stage": row.get("intent_stage") or "unknown",
        "risk": _safe_json(_parse_json(row.get("risk_json"), [])),
        "review_status": row.get("review_status") or "pending",
        "review_reasons": _safe_json(_parse_json(row.get("review_reasons_json"), [])),
        "split": row.get("split") or "unassigned",
        "created_at": row.get("created_at"),
    }


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


class _ClosingConnection(sqlite3.Connection):
    """Make ``with self._db()`` close, not merely commit, SQLite handles."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        suppress = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return suppress


class WeChatCSHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        handler_class: type,
        *,
        db_path: Path,
        token: Optional[str],
        cors_origins: Sequence[str],
        allowed_hosts: Sequence[str],
    ) -> None:
        super().__init__(server_address, handler_class)
        self.db_path = Path(db_path)
        self.api_token = token or ""
        self.cors_origins = {origin.rstrip("/") for origin in cors_origins if origin}
        self.allowed_hosts = {str(host).strip().lower() for host in allowed_hosts if str(host).strip()}
        self.db_write_lock = threading.Lock()


class ApiHandler(BaseHTTPRequestHandler):
    server: WeChatCSHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Access logs intentionally contain no IP, query string, body or content.
        method = getattr(self, "command", "-")
        path = urlsplit(getattr(self, "path", "/")).path
        status = getattr(self, "_response_status", "-")
        print("wechat-cs-api method=%s path=%s status=%s" % (method, path, status))

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            self._check_cors()
            self._response_status = HTTPStatus.NO_CONTENT
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_common_headers(api=True)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except ApiError as error:
            self._send_error(error)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            if not path.startswith("/v1/"):
                if method != "GET":
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "此资源不支持该操作")
                self._serve_static(path)
                return

            self._check_cors()
            self._authenticate()
            query = parse_qs(split.query, keep_blank_values=False)

            if method == "GET" and path == "/v1/health":
                self._get_health()
            elif method == "GET" and path == "/v1/action-queue":
                self._get_action_queue(query)
            elif method == "GET" and path.startswith("/v1/action-queue/"):
                action_id = path[len("/v1/action-queue/") :].strip("/")
                self._get_action_queue_item(action_id)
            elif method == "POST" and path.startswith("/v1/action-queue/") and path.endswith("/draft"):
                action_id = path[len("/v1/action-queue/") : -len("/draft")].strip("/")
                self._post_action_draft(action_id)
            elif method == "POST" and path.startswith("/v1/action-queue/") and path.endswith("/feedback"):
                action_id = path[len("/v1/action-queue/") : -len("/feedback")].strip("/")
                self._post_action_feedback(action_id)
            elif method == "GET" and path == "/v1/customer-insights":
                self._get_customer_insights(query)
            elif method == "GET" and path == "/v1/role-calibration":
                self._get_role_calibration(query)
            elif method == "PATCH" and path.startswith("/v1/role-calibration/"):
                self._patch_role_calibration(path[len("/v1/role-calibration/") :])
            elif method == "PATCH" and path.startswith("/v1/customers/") and path.endswith("/identity-binding"):
                customer_key = path[len("/v1/customers/") : -len("/identity-binding")].strip("/")
                self._patch_identity_binding(customer_key)
            elif method == "GET" and path.startswith("/v1/customers/"):
                self._get_customer(path[len("/v1/customers/") :])
            elif method == "GET" and path == "/v1/style-pairs":
                self._get_style_pairs(query)
            elif method == "PATCH" and path.startswith("/v1/style-pairs/"):
                self._patch_style_pair(path[len("/v1/style-pairs/") :])
            elif method == "POST" and path == "/v1/drafts":
                self._post_draft()
            elif method == "POST" and path.startswith("/v1/drafts/") and path.endswith("/feedback"):
                draft_id = path[len("/v1/drafts/") : -len("/feedback")].strip("/")
                self._post_feedback(draft_id)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
        except ApiError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            # No exception text is returned because it may contain paths or data.
            self._send_error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "服务暂时不可用"))

    def _db(self, *, must_exist: bool = True) -> sqlite3.Connection:
        if not self.server.db_path.is_file():
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "本地分析数据尚未生成")
        conn = sqlite3.connect(
            str(self.server.db_path), timeout=5.0, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON" if must_exist else "PRAGMA foreign_keys = ON")
        return conn

    def _check_cors(self) -> None:
        request_host = _request_hostname(self.headers.get("Host", ""))
        if not request_host or request_host not in self.server.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "host_denied", "请求主机已拒绝")
        origin = self.headers.get("Origin", "").rstrip("/")
        if not origin:
            return
        host = self.headers.get("Host", "")
        same_origin = origin in {"http://" + host, "https://" + host}
        if not same_origin and origin not in self.server.cors_origins:
            raise ApiError(HTTPStatus.FORBIDDEN, "origin_denied", "跨站请求已拒绝")

    def _authenticate(self) -> None:
        configured = self.server.api_token
        if not configured:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "auth_not_configured", "访问令牌尚未配置")
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, configured):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized", "需要有效的访问令牌")

    def _read_json(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_body", "请求正文不能为空")
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_body", "请求正文格式错误")
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "请求正文过大")
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "仅接受 JSON 请求")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON 格式错误")
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求正文必须是对象")
        return value

    def _send_common_headers(self, *, api: bool) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if api:
            self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin:
            host = self.headers.get("Host", "")
            if origin in {"http://" + host, "https://" + host} or origin in self.server.cors_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._response_status = status
        self.send_response(status)
        self._send_common_headers(api=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: ApiError) -> None:
        payload: Dict[str, Any] = {"error": {"code": error.code, "message": error.message}}
        payload.update(_safe_json(error.extra))
        self._send_json(error.status, payload)

    def _serve_static(self, path: str) -> None:
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        item = files.get(path)
        if not item:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "页面不存在")
        file_path = STATIC_DIR / item[0]
        if not file_path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "页面尚未安装")
        body = file_path.read_bytes()
        self._response_status = HTTPStatus.OK
        self.send_response(HTTPStatus.OK)
        self._send_common_headers(api=False)
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Type", item[1])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_action_queue(self, query: Mapping[str, List[str]]) -> None:
        unknown = sorted(set(query) - {"profile", "date", "limit"})
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_query", "行动队列查询参数无效")
        profile = self._query_one(query, "profile") or ""
        if not ACTION_PROFILE_RE.fullmatch(profile):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_profile", "客服账号参数无效")
        queue_date = self._query_one(query, "date") or ""
        try:
            parsed_date = dt.date.fromisoformat(queue_date)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_date", "日期必须使用 YYYY-MM-DD")
        if not queue_date or parsed_date.isoformat() != queue_date:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_date", "日期必须使用 YYYY-MM-DD")
        raw_limit = self._query_one(query, "limit")
        try:
            limit = int(raw_limit or DEFAULT_ACTION_QUEUE_LIMIT)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_limit", "行动队列数量无效")
        if limit < 1 or limit > MAX_ACTION_QUEUE_LIMIT:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_limit", "行动队列数量超出范围")

        with self._db() as conn:
            if not _table_exists(conn, "action_queue_items") or not _table_exists(conn, "action_queue_runs"):
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "今日行动队列尚未生成")
            run_row = conn.execute(
                "SELECT status,policy_version,block_reasons_json,freshness_json,counts_json,as_of_at "
                "FROM action_queue_runs WHERE profile_id=? AND queue_date=?",
                (profile, queue_date),
            ).fetchone()
            if run_row is None:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": {
                            "code": "queue_metadata_missing",
                            "message": "今日行动队列元数据缺失，已停止展示行动建议",
                        },
                        "status": "blocked",
                        "block_reasons": ["queue_metadata_missing"],
                        "freshness": {
                            "messages": {"state": "unknown"},
                            "orders": {"state": "unknown"},
                        },
                        "policy_version": None,
                        "generated_at": None,
                        "send_allowed": False,
                    },
                )
                return
            run = dict(run_row)
            freshness = _parse_json(run.get("freshness_json"), None)
            if not isinstance(freshness, Mapping):
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": {
                            "code": "queue_metadata_invalid",
                            "message": "今日行动队列元数据异常，已停止展示行动建议",
                        },
                        "status": "blocked",
                        "block_reasons": ["queue_metadata_invalid"],
                        "freshness": {
                            "messages": {"state": "unknown"},
                            "orders": {"state": "unknown"},
                        },
                        "policy_version": None,
                        "generated_at": None,
                        "send_allowed": False,
                    },
                )
                return
            persisted_reasons = _safe_action_codes(
                _parse_json(run.get("block_reasons_json"), [])
            )
            freshness, messages_fresh, orders_fresh, runtime_reasons = (
                _runtime_action_freshness(freshness, run.get("as_of_at"))
            )
            block_reasons = list(
                dict.fromkeys(persisted_reasons + runtime_reasons)
            )
            stored_status = str(run.get("status") or "blocked")
            if stored_status not in {"ready", "degraded_order_data", "blocked"}:
                stored_status = "blocked"
                block_reasons = list(
                    dict.fromkeys(block_reasons + ["queue_metadata_invalid"])
                )
            global_runtime_reasons = [
                reason
                for reason in runtime_reasons
                if reason not in ACTION_REPLY_LANE_REASONS
            ]
            global_persisted_reasons = [
                reason
                for reason in persisted_reasons
                if reason not in ACTION_REPLY_LANE_REASONS
            ]
            globally_blocked = bool(global_runtime_reasons) or (
                stored_status == "blocked"
                and (bool(global_persisted_reasons) or not persisted_reasons)
            )
            if globally_blocked and not (
                global_runtime_reasons or global_persisted_reasons
            ):
                block_reasons = list(
                    dict.fromkeys(block_reasons + ["queue_blocked"])
                )
            select = (
                "SELECT action_id,customer_key,profile_id,queue_date,lane,priority_score,priority_version,"
                "reason_codes_json,contact_window_json,recommended_action,strategy_version,signals_json,"
                "required_facts_json,missing_facts_json,prohibited_claims_json,draft_json,confidence,"
                "freshness_json,human_confirmation_state FROM action_queue_items "
                "WHERE profile_id=? AND queue_date=? AND lane=? "
            )
            stored_rows = {
                lane: [
                    dict(row)
                    for row in conn.execute(
                        select + "ORDER BY priority_score DESC,action_id",
                        (profile, queue_date, lane),
                    ).fetchall()
                ]
                for lane in ACTION_QUEUE_LANES
            }

        reply_rows = [
            _hide_runtime_order_signals(row, freshness)
            if not orders_fresh
            else dict(row, freshness_json=json.dumps(freshness, separators=(",", ":")))
            for row in stored_rows["reply_now"]
        ]
        proactive_rows = [
            _hide_runtime_order_signals(row, freshness)
            if not orders_fresh
            else dict(row, freshness_json=json.dumps(freshness, separators=(",", ":")))
            for row in stored_rows["proactive_today"]
        ]
        suppressed_rows = [
            _hide_runtime_order_signals(row, freshness)
            if not orders_fresh
            else dict(row, freshness_json=json.dumps(freshness, separators=(",", ":")))
            for row in stored_rows["suppressed"]
        ]

        if globally_blocked:
            reason = (
                global_runtime_reasons[0]
                if global_runtime_reasons
                else global_persisted_reasons[0]
                if global_persisted_reasons
                else "queue_blocked"
            )
            suppressed_rows.extend(
                _runtime_suppress_action(row, reason, freshness)
                for row in reply_rows + proactive_rows
            )
            reply_rows = []
            proactive_rows = []
        elif not messages_fresh:
            reason = next(
                (
                    item
                    for item in runtime_reasons + persisted_reasons
                    if item in ACTION_REPLY_LANE_REASONS
                ),
                "queue_metadata_invalid",
            )
            suppressed_rows.extend(
                _runtime_suppress_action(row, reason, freshness)
                for row in reply_rows
            )
            reply_rows = []
        if not globally_blocked and not orders_fresh:
            still_proactive: List[Dict[str, Any]] = []
            for row in proactive_rows:
                reasons = _safe_action_codes(_parse_json(row.get("reason_codes_json"), []))
                if "promised_followup" in reasons:
                    still_proactive.append(row)
                else:
                    suppressed_rows.append(
                        _runtime_suppress_action(
                            row, "order_snapshot_stale_for_proactive", freshness
                        )
                    )
            proactive_rows = still_proactive

        # The public limit applies only to actionable proactive rows.  Safety
        # explanations are always complete and therefore never paginated away.
        lane_counts = {
            "reply_now": len(reply_rows),
            "proactive_today": len(proactive_rows),
            "suppressed": len(suppressed_rows),
        }
        proactive_rows = proactive_rows[:limit]

        lanes: Dict[str, List[Dict[str, Any]]] = {
            "reply_now": [_public_action_item(dict(row)) for row in reply_rows],
            "proactive_today": [
                _public_action_item(dict(row)) for row in proactive_rows
            ],
            "suppressed": [
                _public_action_item(dict(row)) for row in suppressed_rows
            ],
        }
        items = [item for lane in ACTION_QUEUE_LANES for item in lanes[lane]]
        returned_counts = {lane: len(lanes[lane]) for lane in ACTION_QUEUE_LANES}
        status = stored_status
        if globally_blocked:
            status = "blocked"
        elif not messages_fresh and proactive_rows:
            status = "historical_snapshot_ready"
        elif not messages_fresh:
            status = "blocked"
        elif not orders_fresh and status != "blocked":
            status = "degraded_order_data"
        reply_lane_reasons = [
            reason
            for reason in block_reasons
            if reason in ACTION_REPLY_LANE_REASONS
        ]
        self._send_json(
            HTTPStatus.OK,
            {
                "profile": profile,
                "date": queue_date,
                "status": status,
                "block_reasons": block_reasons,
                "lane_restrictions": {
                    "reply_now": reply_lane_reasons,
                    "proactive_today": [],
                },
                "freshness": _action_safe_json(freshness),
                "data_mode": (
                    "historical_snapshot"
                    if status == "historical_snapshot_ready"
                    else "current_snapshot"
                ),
                "snapshot_cutoff": _action_safe_json(
                    freshness.get("messages", {}).get("snapshot_at")
                ),
                "realtime_reply_available": messages_fresh and not globally_blocked,
                "contact_precheck_required": bool(
                    proactive_rows and not messages_fresh and not globally_blocked
                ),
                "policy_version": _action_safe_json(run.get("policy_version")),
                "generated_at": _action_safe_json(run.get("as_of_at")),
                "items": items,
                "lanes": lanes,
                "counts": lane_counts,
                "returned_counts": returned_counts,
                "total": sum(lane_counts.values()),
                "limit": limit,
                "human_confirmation_required": True,
                "send_allowed": False,
            },
        )

    def _get_action_queue_item(self, action_id: str) -> None:
        row = self._load_action_queue_row(action_id)
        self._send_json(HTTPStatus.OK, {"item": _public_action_item(row), "send_allowed": False})

    @staticmethod
    def _load_action_queue_row_from_conn(
        conn: sqlite3.Connection, action_id: str
    ) -> Dict[str, Any]:
        if not _table_exists(conn, "action_queue_items") or not _table_exists(conn, "action_queue_runs"):
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "今日行动队列尚未生成")
        row = conn.execute(
                "SELECT action_id,run_id,feature_snapshot_id,customer_key,profile_id,queue_date,lane,"
                "priority_score,priority_version,phone_hmac,"
                "reason_codes_json,contact_window_json,recommended_action,strategy_version,signals_json,"
                "required_facts_json,missing_facts_json,prohibited_claims_json,draft_json,confidence,"
                "freshness_json,human_confirmation_state,updated_at "
                "FROM action_queue_items WHERE action_id=?",
                (action_id,),
            ).fetchone()
        if row is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "action_not_found", "行动项不存在")
        run = conn.execute(
            "SELECT status,block_reasons_json,freshness_json,as_of_at "
            "FROM action_queue_runs WHERE profile_id=? AND queue_date=?",
            (row["profile_id"], row["queue_date"]),
        ).fetchone()
        if run is None:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "queue_metadata_missing",
                "今日行动队列元数据缺失，已停止展示行动建议",
            )
        return _runtime_guard_action_row(dict(row), dict(run))

    def _load_action_queue_row(self, action_id: str) -> Dict[str, Any]:
        self._validate_identifier(action_id, "action_id")
        with self._db() as conn:
            return self._load_action_queue_row_from_conn(conn, action_id)

    def _post_action_draft(self, action_id: str) -> None:
        body = self._read_json()
        unknown = sorted(set(body) - {"facts"})
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_draft_request", "草稿请求只接受当前事实")
        row = self._load_action_queue_row(action_id)
        initial_version = _action_version_fingerprint(row)
        required_codes = _fact_codes(_parse_json(row.get("required_facts_json"), []))
        facts = self._validate_action_facts(body.get("facts", {}), required_codes)
        fallback_reason = "rule_only"
        result: Dict[str, Any]
        model_key = os.environ.get("KIMI_API_KEY", "").strip()
        if row.get("lane") == "suppressed":
            fallback_reason = "action_suppressed"
            result = _fallback_action_draft(row, fallback_reason)
        elif not required_codes:
            fallback_reason = "no_grounded_facts"
            result = _fallback_action_draft(row, fallback_reason)
        elif not model_key:
            fallback_reason = "kimi_unavailable"
            result = _fallback_action_draft(row, fallback_reason)
        elif required_codes and set(facts) != set(required_codes):
            fallback_reason = "required_facts_missing"
            result = _fallback_action_draft(row, fallback_reason)
        else:
            skeleton = _fallback_action_draft(row, "kimi_unavailable_or_rejected")
            prohibited_claims = _safe_action_codes(
                _parse_json(row.get("prohibited_claims_json"), [])
            )
            recommended_action = str(row.get("recommended_action") or "").strip().lower()
            if not ACTION_FACT_CODE_RE.fullmatch(recommended_action):
                recommended_action = "manual_review"
            prompt = self._action_draft_prompt(
                skeleton=str(skeleton["text"]),
                facts=facts,
                required_codes=required_codes,
                prohibited_claims=prohibited_claims,
                recommended_action=recommended_action,
            )
            try:
                model_result = self._call_kimi(prompt)
                result = self._normalize_action_draft(
                    model_result,
                    skeleton=str(skeleton["text"]),
                    facts=facts,
                )
            except Exception:
                # Model transport, formatting and grounding failures all fall
                # back to the auditable rule skeleton.  The exception text is
                # neither logged nor returned because it may contain provider
                # details.
                fallback_reason = "kimi_failed_or_rejected"
                result = _fallback_action_draft(row, fallback_reason)

        updated_at = _iso_now()
        next_confirmation_state = str(row.get("human_confirmation_state") or "pending")
        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                current = self._load_action_queue_row_from_conn(conn, action_id)
                if current.get("lane") == "suppressed":
                    result = _fallback_action_draft(current, "action_suppressed")
                    next_confirmation_state = str(
                        current.get("human_confirmation_state") or "pending"
                    )
                elif _action_version_fingerprint(current) != initial_version:
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "action_version_changed",
                        "行动内容已更新，请刷新后重新审核",
                    )
                else:
                    next_confirmation_state = "pending"
                conn.execute(
                    "UPDATE action_queue_items SET draft_json=?,human_confirmation_state=?,updated_at=? "
                    "WHERE action_id=?",
                    (
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        next_confirmation_state,
                        updated_at,
                        action_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        self._send_json(
            HTTPStatus.OK,
            {
                "action_id": _public_action_id(action_id),
                "draft": _action_safe_json(result),
                "human_confirmation_required": True,
                "human_confirmation_state": next_confirmation_state,
                "send_allowed": False,
            },
        )

    def _post_action_feedback(self, action_id: str) -> None:
        self._validate_identifier(action_id, "action_id")
        body = self._read_json()
        unknown = sorted(set(body) - {"outcome", "final_text", "reason_codes", "reviewer"})
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_feedback", "反馈参数无效")
        outcome = str(body.get("outcome") or "").strip().lower()
        if outcome not in SAFE_ACTION_FEEDBACK_OUTCOMES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_outcome", "反馈必须是 adopted、edited 或 rejected")
        final_text = str(body.get("final_text") or "").strip()
        if len(final_text) > 12000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "text_too_long", "最终回复过长")
        if outcome == "edited" and not final_text:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_final_text", "修改后采用时需要填写最终回复")
        reasons = body.get("reason_codes", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list) or any(
            not isinstance(item, str) or not ACTION_FACT_CODE_RE.fullmatch(item.strip().lower())
            for item in reasons
        ):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_reason_codes", "反馈原因码无效")
        reason_codes = list(dict.fromkeys(item.strip().lower() for item in reasons))[:20]
        reviewer = str(body.get("reviewer") or "dashboard").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", reviewer):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_reviewer", "审核人标识无效")

        guarded_row = self._load_action_queue_row(action_id)
        initial_version = _action_version_fingerprint(guarded_row)
        if outcome != "rejected" and guarded_row.get("lane") == "suppressed":
            raise ApiError(
                HTTPStatus.CONFLICT,
                "action_suppressed",
                "当前行动已被安全规则暂停，只能记录拒绝",
            )

        feedback_id = "action_feedback_" + uuid.uuid4().hex
        created_at = _iso_now()
        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                if not _table_exists(conn, "action_queue_feedback"):
                    raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "今日行动队列尚未生成")
                guarded_row = self._load_action_queue_row_from_conn(conn, action_id)
                if outcome != "rejected" and guarded_row.get("lane") == "suppressed":
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "action_suppressed",
                        "当前行动已被安全规则暂停，只能记录拒绝",
                    )
                if _action_version_fingerprint(guarded_row) != initial_version:
                    raise ApiError(
                        HTTPStatus.CONFLICT,
                        "action_version_changed",
                        "行动内容已更新，请刷新后重新审核",
                    )
                conn.execute(
                    "INSERT INTO action_queue_feedback(feedback_id,action_id,outcome,final_text,reason_codes_json,reviewer,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        feedback_id,
                        action_id,
                        outcome,
                        final_text or None,
                        json.dumps(reason_codes, ensure_ascii=False, separators=(",", ":")),
                        reviewer,
                        created_at,
                    ),
                )
                conn.execute(
                    "UPDATE action_queue_items SET human_confirmation_state=?,updated_at=? WHERE action_id=?",
                    (outcome, created_at, action_id),
                )
                conn.commit()
            finally:
                conn.close()
        self._send_json(
            HTTPStatus.CREATED,
            {
                "feedback_id": feedback_id,
                "action_id": _public_action_id(action_id),
                "outcome": outcome,
                "human_confirmation_state": outcome,
                "send_allowed": False,
                "created_at": created_at,
            },
        )

    @staticmethod
    def _validate_action_facts(value: Any, required_codes: Sequence[str]) -> Dict[str, Any]:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_facts", "当前事实必须是对象")
        allowed = set(required_codes)
        facts: Dict[str, Any] = {}
        for raw_code, fact_value in value.items():
            code = str(raw_code).strip().lower()
            if not ACTION_FACT_CODE_RE.fullmatch(code) or code not in allowed:
                raise ApiError(HTTPStatus.BAD_REQUEST, "fact_not_allowed", "事实码不在本行动项白名单内")
            if fact_value is None or isinstance(fact_value, (bytes, bytearray)):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_fact_value", "当前事实值无效")
            if isinstance(fact_value, str) and not fact_value.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_fact_value", "当前事实值不能为空")
            if _contains_private_value(fact_value):
                raise ApiError(HTTPStatus.BAD_REQUEST, "private_fact_rejected", "当前事实包含不可进入模型的私密信息")
            if _contains_forbidden_model_context(fact_value):
                raise ApiError(HTTPStatus.BAD_REQUEST, "fact_context_rejected", "当前事实包含不可进入模型的结果或动作字段")
            try:
                encoded = json.dumps(fact_value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_fact_value", "当前事实值必须是 JSON 数据")
            if len(encoded.encode("utf-8")) > 4000:
                raise ApiError(HTTPStatus.BAD_REQUEST, "fact_too_long", "单项当前事实过长")
            facts[code] = fact_value
        if len(json.dumps(facts, ensure_ascii=False).encode("utf-8")) > 12000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "facts_too_large", "当前事实过多")
        return facts

    @staticmethod
    def _action_draft_prompt(
        *,
        skeleton: str,
        facts: Mapping[str, Any],
        required_codes: Sequence[str],
        prohibited_claims: Any,
        recommended_action: str,
    ) -> List[Dict[str, str]]:
        context = {
            "rule_skeleton": skeleton,
            "current_fact_allowlist": facts,
            "required_fact_codes": list(required_codes),
            "prohibited_claims": prohibited_claims if isinstance(prohibited_claims, list) else [],
            "recommended_action": recommended_action,
        }
        system = (
            "你只负责润色一条人工审核草稿，不能改变动作或新增事实。"
            "所有价格、库存、尺码、优惠、政策、物流和时效内容只能来自 current_fact_allowlist；"
            "不得作保证，不得声称已发送或已执行。仅返回 JSON 对象，字段为 draft_text 和 used_fact_codes。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
        ]

    @staticmethod
    def _normalize_action_draft(
        result: Mapping[str, Any], *, skeleton: str, facts: Mapping[str, Any]
    ) -> Dict[str, Any]:
        if not isinstance(result, Mapping):
            raise ValueError("model result must be an object")
        text = str(result.get("draft_text") or result.get("text") or "").strip()
        if not text or len(text) > 4000 or _contains_private_value(text):
            raise ValueError("model draft is empty, too long, or private")
        raw_codes = result.get(
            "used_fact_codes",
            result.get("grounding_fact_codes", result.get("fact_codes", [])),
        )
        if raw_codes is None:
            raw_codes = []
        if not isinstance(raw_codes, list) or any(not isinstance(item, str) for item in raw_codes):
            raise ValueError("used fact codes are invalid")
        used_codes = list(dict.fromkeys(item.strip().lower() for item in raw_codes))
        if any(code not in facts for code in used_codes):
            raise ValueError("model used a fact outside the allowlist")
        if GUARANTEE_CLAIM_RE.search(text):
            raise ValueError("guaranteed claim rejected")
        if UNGROUNDED_QUALITATIVE_CLAIM_RE.search(text):
            raise ValueError("ungrounded qualitative claim rejected")

        allowed_numbers = set(re.findall(r"\d+(?:\.\d+)?", skeleton))
        allowed_numbers.update(
            re.findall(r"\d+(?:\.\d+)?", json.dumps(facts, ensure_ascii=False))
        )
        generated_numbers = set(re.findall(r"\d+(?:\.\d+)?", text))
        if not generated_numbers.issubset(allowed_numbers):
            raise ValueError("model introduced an ungrounded number")

        for pattern, acceptable_codes in ACTION_DYNAMIC_CLAIMS:
            if not pattern.search(text):
                continue
            grounded = acceptable_codes.intersection(facts).intersection(used_codes)
            if not grounded:
                raise ValueError("model introduced an ungrounded dynamic claim")
            grounding_text = json.dumps(
                {code: facts[code] for code in sorted(grounded)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if re.search(r"(?:待确认|未知|不确定|unknown|null)", grounding_text, re.IGNORECASE):
                raise ValueError("model promoted an unknown fact into a claim")

        inventory_fact = json.dumps(facts.get("inventory", ""), ensure_ascii=False)
        positive_inventory = re.search(r"(?:有现货|现货充足|有库存|库存充足|有货)", text)
        negative_inventory = re.search(r"(?:没货|无货|缺货|补货中)", text)
        if positive_inventory and not (
            facts.get("inventory") is True
            or re.search(r"(?:有现货|现货充足|有库存|库存充足|有货)", inventory_fact)
        ):
            raise ValueError("model reversed the inventory fact")
        if negative_inventory and not (
            facts.get("inventory") is False
            or re.search(r"(?:没货|无货|缺货|补货中)", inventory_fact)
        ):
            raise ValueError("model reversed the inventory fact")

        temporal_tokens = set(re.findall(r"今天|明天|后天", text))
        allowed_temporal_text = json.dumps(
            {
                code: facts[code]
                for code in ("delivery_estimate", "order_status")
                if code in facts
            },
            ensure_ascii=False,
        )
        if any(token not in allowed_temporal_text and token not in skeleton for token in temporal_tokens):
            raise ValueError("model introduced an ungrounded delivery date")

        return {
            "mode": "kimi_polish",
            "text": text,
            "model_used": True,
            "model": os.environ.get("KIMI_MODEL", "kimi-k2.6"),
            "grounding_fact_codes": used_codes,
            "fallback_for": None,
        }

    def _get_health(self) -> None:
        if not self.server.db_path.is_file():
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "status": "unavailable",
                    "snapshot_at": None,
                    "age_days": None,
                    "stale": True,
                    "stale_after_days": STALE_AFTER_DAYS,
                    "counts": {"customers": 0, "messages": 0, "style_pairs": 0, "drafts": 0},
                },
            )
            return
        with self._db() as conn:
            health = get_store_health(conn)
        calibration = health.get("role_calibration") or {}
        counts = dict(health.get("counts") or {})
        counts["role_calibration_total"] = int(calibration.get("total") or 0)
        counts["role_calibration_reviewed"] = int(calibration.get("reviewed") or 0)
        parsed = _parse_timestamp(health.get("snapshot_last_at"))
        self._send_json(
            HTTPStatus.OK,
            {
                "status": health.get("status") or "degraded",
                "snapshot_at": parsed.isoformat(timespec="seconds") if parsed else None,
                "age_days": health.get("snapshot_age_days"),
                "stale": bool(health.get("snapshot_stale", True)),
                "stale_after_days": STALE_AFTER_DAYS,
                "counts": counts,
                "role_calibration": {
                    "total": int(calibration.get("total") or 0),
                    "reviewed": int(calibration.get("reviewed") or 0),
                    "remaining": max(0, int(calibration.get("total") or 0) - int(calibration.get("reviewed") or 0)),
                    "accuracy": calibration.get("accuracy"),
                    "passed": bool(calibration.get("passed")),
                },
                "warnings": [str(item) for item in (health.get("warnings") or [])],
                "source_status": self._knowledge_source_status(),
            },
        )

    def _get_role_calibration(self, query: Mapping[str, List[str]]) -> None:
        limit, offset = self._pagination(query)
        pending = self._query_one(query, "pending")
        where = " WHERE rc.reviewer_role IS NULL" if pending in {"1", "true", "yes"} else ""
        with self._db() as conn:
            if not _table_exists(conn, "role_calibration"):
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "角色校准样本尚未生成")
            total = int(conn.execute("SELECT COUNT(*) FROM role_calibration").fetchone()[0])
            reviewed = int(conn.execute("SELECT COUNT(*) FROM role_calibration WHERE reviewer_role IS NOT NULL").fetchone()[0])
            correct = int(
                conn.execute(
                    "SELECT COUNT(*) FROM role_calibration WHERE reviewer_role IS NOT NULL AND reviewer_role = expected_role"
                ).fetchone()[0]
            )
            filtered = int(
                conn.execute(
                    "SELECT COUNT(*) FROM role_calibration rc" + where
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT rc.calibration_id,rc.source_status,rc.reviewer_role,rc.reviewed_at,"
                "c.display_name,m.timestamp,m.text FROM role_calibration rc "
                "JOIN customers c ON c.customer_key=rc.customer_key "
                "JOIN messages m ON m.message_key=rc.message_key%s "
                "ORDER BY m.timestamp,rc.calibration_id LIMIT ? OFFSET ?" % where,
                (limit, offset),
            ).fetchall()
        accuracy = (correct / reviewed) if reviewed else None
        passed = bool(total >= 200 and reviewed == total and accuracy is not None and accuracy >= 0.99)
        items = []
        for row in rows:
            visible_text, _ = redact_text(row["text"] or "")
            items.append(
                {
                    "calibration_id": row["calibration_id"],
                    "display_name": row["display_name"],
                    "message_text": visible_text,
                    "timestamp": row["timestamp"],
                    "source_status": row["source_status"],
                    "reviewer_role": row["reviewer_role"],
                    "reviewed_at": row["reviewed_at"],
                }
            )
        self._send_json(
            HTTPStatus.OK,
            {
                "items": items,
                "total": filtered,
                "limit": limit,
                "offset": offset,
                "progress": {
                    "total": total,
                    "reviewed": reviewed,
                    "remaining": max(0, total - reviewed),
                    "accuracy": accuracy,
                    "passed": passed,
                },
            },
        )

    def _patch_role_calibration(self, calibration_id: str) -> None:
        self._validate_identifier(calibration_id, "calibration_id")
        body = self._read_json()
        reviewer_role = str(body.get("reviewer_role") or "").strip().lower()
        if reviewer_role not in {"studio", "customer"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_role", "角色必须是 studio 或 customer")
        reviewed_at = _iso_now()
        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                cursor = conn.execute(
                    "UPDATE role_calibration SET reviewer_role=?,reviewed_at=? WHERE calibration_id=?",
                    (reviewer_role, reviewed_at, calibration_id),
                )
                if cursor.rowcount != 1:
                    raise ApiError(HTTPStatus.NOT_FOUND, "calibration_not_found", "校准样本不存在")
                conn.commit()
                total = int(conn.execute("SELECT COUNT(*) FROM role_calibration").fetchone()[0])
                reviewed = int(conn.execute("SELECT COUNT(*) FROM role_calibration WHERE reviewer_role IS NOT NULL").fetchone()[0])
                correct = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM role_calibration WHERE reviewer_role IS NOT NULL AND reviewer_role=expected_role"
                    ).fetchone()[0]
                )
            finally:
                conn.close()
        accuracy = (correct / reviewed) if reviewed else None
        self._send_json(
            HTTPStatus.OK,
            {
                "item": {
                    "calibration_id": calibration_id,
                    "reviewer_role": reviewer_role,
                    "reviewed_at": reviewed_at,
                },
                "progress": {
                    "total": total,
                    "reviewed": reviewed,
                    "remaining": max(0, total - reviewed),
                    "accuracy": accuracy,
                    "passed": bool(total >= 200 and reviewed == total and accuracy is not None and accuracy >= 0.99),
                },
            },
        )

    def _get_customer_insights(self, query: Mapping[str, List[str]]) -> None:
        limit, offset = self._pagination(query)
        level = self._query_one(query, "level")
        aftersales = self._query_one(query, "aftersales")
        clauses: List[str] = []
        params: List[Any] = []
        if level:
            if level not in {"high", "medium", "low"}:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_level", "机会等级无效")
            clauses.append("opportunity_level = ?")
            params.append(level)
        if aftersales in {"1", "true", "yes"}:
            clauses.append("aftersales_priority IS NOT NULL AND aftersales_priority != ''")
        elif aftersales in {"0", "false", "no"}:
            clauses.append("(aftersales_priority IS NULL OR aftersales_priority = '')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as conn:
            if not _table_exists(conn, "customers"):
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "客户分析尚未生成")
            total = int(conn.execute("SELECT COUNT(*) FROM customers" + where, params).fetchone()[0])
            rows = conn.execute(
                "SELECT customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
                "aftersales_priority,summary,reasons_json,evidence_json,memory_json "
                "FROM customers%s ORDER BY CASE opportunity_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                "opportunity_score DESC,last_active_at DESC LIMIT ? OFFSET ?" % where,
                params + [limit, offset],
            ).fetchall()
            raw_rows = [dict(row) for row in rows]
            self._attach_binding_summaries(conn, raw_rows)
        self._send_json(HTTPStatus.OK, {"items": [_public_customer(row) for row in raw_rows], "total": total, "limit": limit, "offset": offset})

    def _get_customer(self, customer_key: str) -> None:
        self._validate_identifier(customer_key, "customer_key")
        with self._db() as conn:
            row = conn.execute(
                "SELECT customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
                "aftersales_priority,summary,reasons_json,evidence_json,memory_json FROM customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            if row is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "customer_not_found", "客户不存在")
            customer_data = dict(row)
            self._attach_binding_summaries(conn, [customer_data])
            messages: List[Dict[str, Any]] = []
            if _table_exists(conn, "messages"):
                message_rows = conn.execute(
                    "SELECT message_key,role,timestamp,text FROM messages WHERE customer_key = ? "
                    "ORDER BY timestamp DESC,source_ordinal DESC LIMIT 50",
                    (customer_key,),
                ).fetchall()
                messages = [
                    {
                        "message_key": item["message_key"],
                        "role": item["role"],
                        "timestamp": item["timestamp"],
                        "text": redact_text(item["text"] or "")[0],
                    }
                    for item in reversed(message_rows)
                ]
            bindings = []
            if _table_exists(conn, "identity_bindings"):
                binding_rows = conn.execute(
                    "SELECT binding_id,masked_hint,state,evidence_message_keys_json,reviewed_at "
                    "FROM identity_bindings WHERE customer_key=? AND state!='missing' ORDER BY state,masked_hint",
                    (customer_key,),
                ).fetchall()
                bindings = [
                    {
                        "binding_id": item["binding_id"],
                        "masked_hint": item["masked_hint"],
                        "state": item["state"],
                        "evidence_message_keys": _parse_json(item["evidence_message_keys_json"], []),
                        "reviewed_at": item["reviewed_at"],
                    }
                    for item in binding_rows
                ]
        self._send_json(HTTPStatus.OK, {"customer": _public_customer(customer_data), "messages": messages, "identity_candidates": bindings})

    @staticmethod
    def _attach_binding_summaries(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
        if not rows or not _table_exists(conn, "identity_bindings"):
            return
        keys = [row["customer_key"] for row in rows]
        placeholders = ",".join("?" for _ in keys)
        binding_rows = conn.execute(
            "SELECT customer_key,state,phone_hmac FROM identity_bindings WHERE customer_key IN (%s)" % placeholders,
            keys,
        ).fetchall()
        grouped: Dict[str, List[sqlite3.Row]] = {}
        for binding in binding_rows:
            grouped.setdefault(binding["customer_key"], []).append(binding)
        priority = {
            "approved": 0,
            "ambiguous_shared": 1,
            "review": 2,
            "candidate_unique": 3,
            "rejected": 4,
            "missing": 5,
        }
        for row in rows:
            candidates = grouped.get(row["customer_key"], [])
            states = [item["state"] for item in candidates]
            state = min(states, key=lambda item: priority.get(item, 99)) if states else "unmatched"
            row["identity_binding_state"] = state
            row["identity_candidate_count"] = len(
                {
                    item["phone_hmac"]
                    for item in candidates
                    if item["phone_hmac"] and item["state"] not in {"missing", "rejected"}
                }
            )

    def _patch_identity_binding(self, customer_key: str) -> None:
        self._validate_identifier(customer_key, "customer_key")
        body = self._read_json()
        binding_id = str(body.get("binding_id") or "").strip()
        action = str(body.get("action") or "").strip().lower()
        self._validate_identifier(binding_id, "binding_id")
        if action not in {"approve", "reject"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_action", "身份候选只能 approve 或 reject")
        reviewed_at = _iso_now()
        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                if not _table_exists(conn, "identity_bindings"):
                    raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "身份候选尚未生成")
                selected = conn.execute(
                    "SELECT binding_id,state FROM identity_bindings WHERE binding_id=? AND customer_key=? AND phone_hmac IS NOT NULL",
                    (binding_id, customer_key),
                ).fetchone()
                if selected is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "binding_not_found", "身份候选不存在")
                if action == "approve":
                    conn.execute(
                        "UPDATE identity_bindings SET state='rejected',reviewed_at=? "
                        "WHERE customer_key=? AND binding_id!=? AND phone_hmac IS NOT NULL",
                        (reviewed_at, customer_key, binding_id),
                    )
                    new_state = "approved"
                else:
                    new_state = "rejected"
                conn.execute(
                    "UPDATE identity_bindings SET state=?,reviewed_at=? WHERE binding_id=? AND customer_key=?",
                    (new_state, reviewed_at, binding_id, customer_key),
                )
                conn.commit()
            finally:
                conn.close()
        self._send_json(
            HTTPStatus.OK,
            {"item": {"binding_id": binding_id, "state": new_state, "reviewed_at": reviewed_at}},
        )

    def _get_style_pairs(self, query: Mapping[str, List[str]]) -> None:
        limit, offset = self._pagination(query)
        status = self._query_one(query, "status")
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            if status not in SAFE_REVIEW_STATUSES:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_status", "审核状态无效")
            clauses.append("review_status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as conn:
            if not _table_exists(conn, "style_pairs"):
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "风格样本尚未生成")
            total = int(conn.execute("SELECT COUNT(*) FROM style_pairs" + where, params).fetchone()[0])
            rows = conn.execute(
                "SELECT pair_id,customer_key,trigger_text,reply_text,context_json,intent_stage,risk_json,"
                "review_status,review_reasons_json,split,created_at FROM style_pairs%s "
                "ORDER BY CASE review_status WHEN 'pending' THEN 0 ELSE 1 END,created_at DESC LIMIT ? OFFSET ?" % where,
                params + [limit, offset],
            ).fetchall()
        self._send_json(HTTPStatus.OK, {"items": [_public_style_pair(dict(row)) for row in rows], "total": total, "limit": limit, "offset": offset})

    def _patch_style_pair(self, pair_id: str) -> None:
        self._validate_identifier(pair_id, "pair_id")
        body = self._read_json()
        status = str(body.get("review_status") or "").strip().lower()
        if status not in SAFE_REVIEW_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_status", "审核状态必须是 pending、approved 或 rejected")
        reasons = body.get("review_reasons", body.get("reasons", []))
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_reasons", "审核原因必须是文本数组")
        reasons = [item.strip()[:240] for item in reasons if item.strip()][:20]
        reviewer = str(body.get("reviewer") or "dashboard")[:64]

        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                row = conn.execute("SELECT pair_id FROM style_pairs WHERE pair_id = ?", (pair_id,)).fetchone()
                if row is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "pair_not_found", "样本不存在")
                conn.execute(
                    "UPDATE style_pairs SET review_status = ?,review_reasons_json = ? WHERE pair_id = ?",
                    (status, json.dumps(reasons, ensure_ascii=False), pair_id),
                )
                if _table_exists(conn, "reviews"):
                    conn.execute(
                        "INSERT INTO reviews(review_id,pair_id,verdict,reasons_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                        ("review_" + uuid.uuid4().hex, pair_id, status, json.dumps(reasons, ensure_ascii=False), reviewer, _iso_now()),
                    )
                conn.commit()
                updated = conn.execute(
                    "SELECT pair_id,customer_key,trigger_text,reply_text,context_json,intent_stage,risk_json,"
                    "review_status,review_reasons_json,split,created_at FROM style_pairs WHERE pair_id = ?",
                    (pair_id,),
                ).fetchone()
            finally:
                conn.close()
        self._send_json(HTTPStatus.OK, {"item": _public_style_pair(dict(updated))})

    def _post_draft(self) -> None:
        body = self._read_json()
        customer_key = str(body.get("customer_key") or "").strip()
        request_text = str(body.get("latest_message") or body.get("request_text") or "").strip()
        self._validate_identifier(customer_key, "customer_key")
        if not request_text:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_message", "请粘贴客户最新消息")
        if len(request_text) > 8000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "message_too_long", "客户消息过长")
        order_context = body.get("order_context")
        if order_context is not None and not isinstance(order_context, (dict, list)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_order_context", "订单上下文必须是对象或数组")
        request_text, request_redactions = redact_text(request_text)

        kimi_key = os.environ.get("KIMI_API_KEY", "").strip()
        if not kimi_key:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "kimi_unavailable",
                "Kimi API Key 尚未配置，未生成任何模拟回复",
                grounding_missing=True,
            )

        with self._db() as conn:
            customer_row = conn.execute(
                "SELECT customer_key,display_name,last_active_at,opportunity_score,opportunity_level,"
                "aftersales_priority,summary,reasons_json,evidence_json,memory_json FROM customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            if customer_row is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "customer_not_found", "客户不存在")
            recent_rows = conn.execute(
                "SELECT role,timestamp,text FROM messages WHERE customer_key = ? ORDER BY timestamp DESC,source_ordinal DESC LIMIT 20",
                (customer_key,),
            ).fetchall() if _table_exists(conn, "messages") else []
            style_candidates = conn.execute(
                "SELECT pair_id,customer_key,trigger_text,reply_text,intent_stage,risk_json,created_at FROM style_pairs "
                "WHERE review_status = 'approved' ORDER BY created_at DESC LIMIT 200"
            ).fetchall() if _table_exists(conn, "style_pairs") else []

        customer = _public_customer(dict(customer_row))
        memory = customer.get("memory") if isinstance(customer.get("memory"), dict) else {}
        grounding_refs = self._grounding_refs(memory)
        knowledge = self._search_knowledge(request_text)
        grounding_refs.extend(knowledge["refs"])
        safe_order_context = _redact_strings(order_context) if order_context is not None else None
        if safe_order_context:
            grounding_refs.append(
                {
                    "id": "order:manual-context",
                    "type": "order_context",
                    "title": "本次人工提供的订单上下文",
                    "facts": safe_order_context,
                }
            )
        grounding_refs = grounding_refs[:20]
        dynamic_question = bool(DYNAMIC_FACT_RE.search(request_text))
        # Knowledge may be intentionally disabled for ordinary style-only
        # replies.  Fail closed only when the question actually depends on a
        # volatile/policy fact and no current fact was found.
        grounding_missing = bool(dynamic_question and not grounding_refs)
        scored_styles = []
        for candidate in style_candidates:
            risk = _parse_json(candidate["risk_json"], {})
            if not isinstance(risk, dict) or risk.get("level") != "low":
                continue
            score = _jaccard_similarity(request_text, candidate["trigger_text"] or "")
            if candidate["customer_key"] == customer_key:
                score += 0.08
            scored_styles.append((score, candidate["created_at"] or "", dict(candidate)))
        scored_styles.sort(key=lambda item: (item[0], item[1]), reverse=True)
        style_rows = [item[2] for item in scored_styles[:5]]
        prompt = self._draft_prompt(
            customer=customer,
            recent=list(reversed([dict(row) for row in recent_rows])),
            styles=[dict(row) for row in style_rows],
            request_text=request_text,
            grounding_refs=grounding_refs,
            grounding_missing=grounding_missing,
        )
        result = self._call_kimi(prompt)
        result = self._normalize_draft_result(result, grounding_refs, grounding_missing)
        draft_id = "draft_" + uuid.uuid4().hex
        created_at = _iso_now()

        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(
                    "INSERT INTO drafts(draft_id,customer_key,request_text,draft_text,intent,needs_clarification,needs_human,"
                    "risk_json,grounding_refs_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        draft_id,
                        customer_key,
                        request_text,
                        result["draft_text"],
                        result["intent"],
                        int(result["needs_clarification"]),
                        int(result["needs_human"]),
                        json.dumps(result["risk_flags"], ensure_ascii=False),
                        json.dumps(result["grounding_refs"], ensure_ascii=False),
                        "generated",
                        created_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        payload = {
            "draft_id": draft_id,
            "created_at": created_at,
            "grounding_missing": grounding_missing,
            "style_examples_used": len(style_rows),
            "input_redaction_flags": request_redactions,
            "source_status": knowledge["source_status"],
        }
        payload.update(result)
        self._send_json(HTTPStatus.CREATED, payload)

    def _post_feedback(self, draft_id: str) -> None:
        self._validate_identifier(draft_id, "draft_id")
        body = self._read_json()
        outcome = str(body.get("outcome") or "").strip().lower()
        if outcome not in SAFE_FEEDBACK_OUTCOMES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_outcome", "反馈必须是 adopted、edited 或 rejected")
        final_text = str(body.get("final_text") or "").strip()
        if len(final_text) > 12000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "text_too_long", "最终回复过长")
        if outcome == "edited" and not final_text:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_final_text", "修改后采用时需要填写最终回复")

        feedback_id = "feedback_" + uuid.uuid4().hex
        created_at = _iso_now()
        stored_outcome = "accepted" if outcome == "adopted" else outcome
        with self.server.db_write_lock:
            conn = self._db(must_exist=False)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                row = conn.execute("SELECT customer_key FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
                if row is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "draft_not_found", "草稿不存在")
                table = "feedback" if _table_exists(conn, "feedback") else "draft_feedback"
                conn.execute(
                    'INSERT INTO "%s"(feedback_id,draft_id,customer_key,outcome,final_text,created_at) VALUES(?,?,?,?,?,?)' % table,
                    (feedback_id, draft_id, row["customer_key"], stored_outcome, final_text, created_at),
                )
                conn.execute("UPDATE drafts SET status = ? WHERE draft_id = ?", (stored_outcome, draft_id))
                conn.commit()
            finally:
                conn.close()
        self._send_json(HTTPStatus.CREATED, {"feedback_id": feedback_id, "draft_id": draft_id, "outcome": outcome, "created_at": created_at})

    def _draft_prompt(
        self,
        *,
        customer: Mapping[str, Any],
        recent: Sequence[Mapping[str, Any]],
        styles: Sequence[Mapping[str, Any]],
        request_text: str,
        grounding_refs: Sequence[Mapping[str, Any]],
        grounding_missing: bool,
    ) -> List[Dict[str, str]]:
        context = {
            "customer_summary": customer.get("summary"),
            "customer_memory": customer.get("memory"),
            "recent_messages": [
                {
                    "role": row.get("role"),
                    "timestamp": row.get("timestamp"),
                    "text": redact_text(str(row.get("text") or ""))[0],
                }
                for row in recent
            ],
            "approved_style_examples": [
                {
                    "example_id": row.get("pair_id"),
                    "customer": row.get("trigger_text"),
                    "studio": row.get("reply_text"),
                    "stage": row.get("intent_stage"),
                }
                for row in styles
            ],
            "grounding_refs": [
                {key: value for key, value in ref.items() if key != "source_ref"}
                for ref in grounding_refs
            ],
            "grounding_missing": grounding_missing,
            "latest_customer_message": request_text,
        }
        system = (
            "你是工作室的微信私聊回复起草助手。只生成给人工审核的建议，不声称已经执行任何操作。"
            "模仿示例的语气、节奏和长度，但绝不能复制旧客户姓名、数字、承诺或历史事实。"
            "价格、库存、退款、补发、赔付、物流、到货时间只能使用 grounding_refs 中明确给出的当前事实。"
            "缺少依据时要追问或标记人工确认，不得编造。遇到投诉、退款、赔偿和高风险承诺应 needs_human=true。"
            "仅返回一个 JSON 对象，字段为 draft_text、intent、needs_clarification、needs_human、risk_flags、grounding_refs；"
            "risk_flags 和 grounding_refs 必须是数组。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
        ]

    @staticmethod
    def _knowledge_registry() -> Optional[KnowledgeRegistry]:
        configured = os.environ.get("WECHAT_CS_KNOWLEDGE_CONFIG", "").strip()
        if configured:
            path = Path(configured).expanduser()
        else:
            path = ROOT / "config" / "knowledge_sources.json"
            if not path.is_file():
                path = ROOT / "config" / "knowledge_sources.example.json"
        try:
            return KnowledgeRegistry(path, workspace_root=ROOT)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _knowledge_source_status(self) -> List[Dict[str, Any]]:
        registry = self._knowledge_registry()
        if registry is None:
            return []
        try:
            return list(_safe_json(registry.source_status()))
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _search_knowledge(self, query: str) -> Dict[str, Any]:
        registry = self._knowledge_registry()
        if registry is None:
            return {"refs": [], "source_status": [], "grounding_missing": True}
        try:
            result = registry.search(query, limit=8)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"refs": [], "source_status": self._knowledge_source_status(), "grounding_missing": True}
        refs = []
        for hit in result.get("hits", []):
            if not isinstance(hit, Mapping):
                continue
            digest = hashlib.sha256(
                (str(hit.get("source_id")) + "\x1f" + str(hit.get("title")) + "\x1f" + str(hit.get("content"))).encode("utf-8")
            ).hexdigest()[:16]
            refs.append(
                {
                    "id": "knowledge:%s:%s" % (hit.get("source_id") or "source", digest),
                    "type": hit.get("source_type") or "knowledge",
                    "title": hit.get("title") or "知识依据",
                    "content": redact_text(str(hit.get("content") or "")[:1200])[0],
                    "source_ref": hit.get("source_ref") or "",
                    "structured": _safe_json(hit.get("structured") or {}),
                }
            )
        return {
            "refs": refs,
            "source_status": _safe_json(result.get("sources") or []),
            "grounding_missing": bool(result.get("grounding_missing", True)),
        }

    def _call_kimi(self, messages: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
        base = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.cn/v1").rstrip("/")
        model = os.environ.get("KIMI_MODEL", "kimi-k2.6")
        request_payload = {
            "model": model,
            "messages": list(messages),
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + os.environ["KIMI_API_KEY"].strip(),
                "Content-Type": "application/json",
                "User-Agent": "wechat-cs-local/1.0",
            },
            method="POST",
        )
        timeout = float(os.environ.get("KIMI_TIMEOUT_SECONDS", "45"))
        try:
            with urllib.request.urlopen(request, timeout=max(5.0, min(timeout, 120.0))) as response:
                raw = response.read(MAX_BODY_BYTES)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise ApiError(
                HTTPStatus.BAD_GATEWAY,
                "kimi_request_failed",
                "Kimi 暂时不可用，未生成任何模拟回复",
                grounding_missing=True,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
            text = str(content).strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_GATEWAY, "kimi_invalid_response", "Kimi 返回格式异常，未保存草稿", grounding_missing=True)
        if not isinstance(result, dict):
            raise ApiError(HTTPStatus.BAD_GATEWAY, "kimi_invalid_response", "Kimi 返回格式异常，未保存草稿", grounding_missing=True)
        return result

    @staticmethod
    def _normalize_draft_result(
        result: Mapping[str, Any],
        grounding_refs: Sequence[Mapping[str, Any]],
        grounding_missing: bool,
    ) -> Dict[str, Any]:
        text = str(result.get("draft_text") or "").strip()
        if not text:
            raise ApiError(HTTPStatus.BAD_GATEWAY, "kimi_invalid_response", "Kimi 未返回可用草稿", grounding_missing=True)
        if len(text) > 12000:
            text = text[:12000]
        risks = result.get("risk_flags", [])
        risks = [str(item)[:120] for item in risks] if isinstance(risks, list) else [str(risks)[:120]]
        if grounding_missing and "grounding_missing" not in risks:
            risks.append("grounding_missing")
        allowed_ref_ids = {str(item.get("id")) for item in grounding_refs if isinstance(item, Mapping) and item.get("id")}
        requested_refs = result.get("grounding_refs", [])
        requested_ids = {str(item) for item in requested_refs} if isinstance(requested_refs, list) else set()
        safe_refs = [item for item in grounding_refs if str(item.get("id")) in requested_ids or not requested_ids]
        return {
            "draft_text": text,
            "intent": str(result.get("intent") or "unknown")[:120],
            "needs_clarification": bool(result.get("needs_clarification")) or grounding_missing,
            "needs_human": bool(result.get("needs_human")) or grounding_missing,
            "risk_flags": risks[:20],
            "grounding_refs": _safe_json(safe_refs),
        }

    @staticmethod
    def _grounding_refs(memory: Mapping[str, Any]) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        for key in ("grounding_refs", "knowledge_refs", "order_refs", "current_facts"):
            value = memory.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping) and item.get("id"):
                        refs.append(dict(_safe_json(item)))
        return refs[:20]

    @staticmethod
    def _query_one(query: Mapping[str, List[str]], name: str) -> Optional[str]:
        values = query.get(name)
        return values[0].strip().lower() if values else None

    def _pagination(self, query: Mapping[str, List[str]]) -> Tuple[int, int]:
        try:
            limit = int(self._query_one(query, "limit") or DEFAULT_LIMIT)
            offset = int(self._query_one(query, "offset") or 0)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_pagination", "分页参数无效")
        if limit < 1 or limit > MAX_LIMIT or offset < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_pagination", "分页参数超出范围")
        return limit, offset

    @staticmethod
    def _validate_identifier(value: str, name: str) -> None:
        if not value or len(value) > 160 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid_identifier", "%s 无效" % name)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: Optional[os.PathLike] = None,
    token: Optional[str] = None,
) -> WeChatCSHTTPServer:
    resolved_token = token if token is not None else os.environ.get("WECHAT_CS_TOKEN", "")
    if not resolved_token:
        raise RuntimeError("WECHAT_CS_TOKEN is required for local and remote access")
    if not _is_loopback(host) and len(resolved_token) < 32:
        raise RuntimeError("WECHAT_CS_TOKEN must contain at least 32 characters when binding to a non-loopback address")
    cors = [item.strip() for item in os.environ.get("WECHAT_CS_CORS_ORIGINS", "").split(",") if item.strip()]
    allowed_hosts = [
        item.strip().lower()
        for item in os.environ.get("WECHAT_CS_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    if _is_loopback(host):
        allowed_hosts.extend(["127.0.0.1", "localhost", "localhost.localdomain", "::1"])
    elif host not in {"0.0.0.0", "::"}:
        allowed_hosts.append(str(host).strip().lower())
    if not allowed_hosts:
        raise RuntimeError(
            "WECHAT_CS_ALLOWED_HOSTS is required when binding to a wildcard address"
        )
    return WeChatCSHTTPServer(
        (host, int(port)),
        ApiHandler,
        db_path=Path(db_path) if db_path else DEFAULT_DB_PATH,
        token=resolved_token,
        cors_origins=cors,
        allowed_hosts=allowed_hosts,
    )


def serve(host: str = "127.0.0.1", port: int = 8765, db_path: Optional[os.PathLike] = None) -> None:
    server = create_server(host=host, port=port, db_path=db_path)
    try:
        print("wechat-cs-api listening on http://%s:%d" % server.server_address)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local WeChat customer-service review API")
    parser.add_argument("--host", default=os.environ.get("WECHAT_CS_BIND", os.environ.get("WECHAT_CS_HOST", "127.0.0.1")))
    parser.add_argument("--port", default=int(os.environ.get("WECHAT_CS_PORT", "8765")), type=int)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
