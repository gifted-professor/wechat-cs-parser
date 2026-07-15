"""Internal, review-only sales workbench for the frozen 50-person pilot.

The workbench keeps a deliberately small surface: private-network customer facts,
frozen order history, business priority, evidence excerpts, and opening-line
review.  It has no drafting trigger, model trigger, or send route.  Clear-text
customer identity is resolved at the local display edge and is never written
back to the profile database or model artifacts.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .identity import global_phone_hmac, normalize_phone


STATIC_DIR = Path(__file__).with_name("review_portal_static")
DEFAULT_DB_PATH = Path(
    "/Volumes/GPFS/Users/a1234/Desktop/Coding/wechat-cs-parser/.wechat-cs/"
    "runs/20260713T140730+0800-833c3257/wechat_cs_m0.sqlite3"
)
DEFAULT_CUSTOMER_DATA_PATH = Path(
    "/Volumes/GPFS/Users/a1234/Desktop/dashboard/customer_action_data.json"
)
DEFAULT_HMAC_SECRET_PATH = Path(
    "/Volumes/GPFS/Users/a1234/Desktop/Coding/wechat-cs-parser/"
    ".wechat-cs/config/hmac_secret"
)
MAX_BODY_BYTES = 64 * 1024
DEFAULT_MESSAGE_LIMIT = 20
MAX_MESSAGE_LIMIT = 100
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
SHARED_REVIEWER_KEY = "operator-shared-workbench"
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
SEPARATED_PHONE = re.compile(
    r"(?<!\d)(?:\+?86[\s\-·•._()]*)?1[\s\-·•._()]*[3-9]"
    r"(?:[\s\-·•._()]*\d){9}(?!\d)"
)
SEPARATED_IDENTITY = re.compile(
    r"(?<!\d)(?:\d[\s\-·•._()]*){17}[\dXx](?![\dXx])"
)
EVENT_ID = re.compile(r"sales-profile-event-[a-f0-9]{12,}")
TECHNICAL_REF = re.compile(
    r"(?:message[_-][A-Za-z0-9]+|order-line[_-][A-Za-z0-9]+|sales-profile-event-[A-Za-z0-9]+)"
)
PRIORITY_ASSESSMENTS = frozenset(
    {"", "accurate", "too_high", "too_low", "not_suitable"}
)
PRIORITY_REASON_CODES = frozenset(
    {
        "",
        "clear_intent",
        "repurchase_potential",
        "no_recent_need",
        "refuses_marketing",
        "unresolved_aftersales",
        "recently_purchased",
        "price_resistance",
        "purchased_elsewhere",
        "insufficient_chat_signal",
        "other",
    }
)
REVIEW_REQUIRED_FIELDS = frozenset(
    {"card_version", "verdict", "suggested_opening"}
)
REVIEW_OPTIONAL_FIELDS = (
    "priority_assessment",
    "priority_reason_code",
    "priority_note",
    "evidence_message_ref",
    "revision_notes",
)

EVIDENCE_FIELD_LABELS = {
    "sku_name": "商品",
    "brand": "品牌",
    "category": "品类",
    "color": "颜色",
    "size": "尺码",
    "order_note": "订单备注",
    "paid_on": "付款日期",
    "refund_type": "售后类型",
    "refund_on": "售后日期",
    "refund_fact_at_cutoff": "截止点售后事实",
}

AFTERSALES_LABELS = {
    "cancel": "订单取消（不计售后）",
    "return": "退货退款",
    "return_taro": "退芋圆",
    "exchange": "换货",
    "compensation": "补偿处理",
    "other": "其他售后",
}
BUSINESS_AFTERSALES_TYPES = frozenset(
    {"return", "return_taro", "exchange", "compensation", "other"}
)
PERIOD_LABELS = {
    "morning": "上午",
    "noon": "中午",
    "afternoon": "下午",
    "evening": "晚上",
}
EVENT_LABELS = {
    "brand_preference": "品牌偏好",
    "product_preference": "商品偏好",
    "aftersales": "售后信号",
    "delayed_purchase": "延迟购买",
    "price_hesitation": "价格犹豫",
    "stock_wait": "等待到货",
    "birthday_clue": "生日线索",
    "relationship_signal": "关系信号",
    "promotion_or_payday_wait": "活动或发薪等待",
    "future_return": "未来回访",
    "contact_refusal": "拒绝联系",
}
FUTURE_EVENT_TYPES = frozenset(
    {"future_return", "delayed_purchase", "stock_wait", "promotion_or_payday_wait"}
)

STRATUM_LABELS = {
    "complex_risk": "售后关怀",
    "future_return_wait": "回访等待",
    "high_frequency": "高频客户",
    "high_value": "高价值客户",
    "dormant_repeat": "沉睡复购",
    "control": "普通对照",
}
STRATUM_ORDER = {
    "complex_risk": 1,
    "future_return_wait": 2,
    "high_frequency": 3,
    "high_value": 4,
    "dormant_repeat": 5,
    "control": 6,
}


class PortalError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _translate_business_text(value: Any) -> str:
    text = str(value or "")
    for token, label in sorted(AFTERSALES_LABELS.items(), key=lambda item: -len(item[0])):
        text = re.sub(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(token), label, text)
    for token, label in {
        "RFM": "历史购买表现",
        "Frequency": "购买次数",
        "Recency": "距上次购买",
        "Monetary": "累计消费",
        "quality_flags": "需要核对的数据",
    }.items():
        text = re.sub(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(token), label, text, flags=re.IGNORECASE)
    return text


def _clean_text(value: Any) -> str:
    text = str(value or "")
    text = SEPARATED_PHONE.sub("[手机号已隐藏]", text)
    text = SEPARATED_IDENTITY.sub("[身份信息已隐藏]", text)
    text = PHONE.sub("[手机号已隐藏]", text)
    text = IDENTITY.sub("[身份信息已隐藏]", text)
    text = EVENT_ID.sub("[已验证证据]", text)
    text = TECHNICAL_REF.sub("[记录编号已隐藏]", text)
    return _translate_business_text(text)


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    return value


def _bounded_plain_text(value: Any, *, limit: int = 120) -> str:
    text = "".join(
        character
        for character in str(value or "").strip()
        if character >= " " and character != "\x7f"
    )
    return re.sub(r"\s+", " ", text)[:limit]


def _mask_phone(value: Any) -> str:
    phone = normalize_phone(str(value or ""))
    return "%s****%s" % (phone[:3], phone[-4:]) if phone else "手机号待补全"


def _load_customer_index(
    customer_data_path: Optional[Path],
    hmac_secret_path: Optional[Path],
) -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    if customer_data_path is None or hmac_secret_path is None:
        return {}, None
    data_path = Path(customer_data_path).expanduser().resolve()
    secret_path = Path(hmac_secret_path).expanduser().resolve()
    if not data_path.is_file() or not secret_path.is_file():
        return {}, None
    secret = secret_path.read_text(encoding="utf-8").strip()
    if len(secret) < 20:
        raise RuntimeError("customer identity HMAC secret is invalid")
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("customer identity source is invalid") from exc
    records = payload.get("customers") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("customer identity source has no customers list")
    index: Dict[str, Dict[str, str]] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        phone = normalize_phone(str(row.get("phone") or ""))
        if phone is None:
            continue
        phone_hmac = global_phone_hmac(secret, phone)
        candidate = {
            "name": _bounded_plain_text(row.get("customer_name"), limit=80),
            "phone": phone,
        }
        previous = index.get(phone_hmac)
        if previous is None or (not previous.get("name") and candidate["name"]):
            index[phone_hmac] = candidate
    synced_at = _bounded_plain_text(payload.get("generated_at"), limit=64) if isinstance(payload, dict) else ""
    return index, synced_at or None


def _ensure_opening_review_schema(db_path: Path) -> None:
    if not db_path.is_file():
        return
    connection = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sales_profile_opening_reviews (
                review_id TEXT PRIMARY KEY,
                sales_profile_id TEXT NOT NULL
                    REFERENCES sales_profiles(sales_profile_id) ON DELETE CASCADE,
                card_version TEXT NOT NULL,
                verdict TEXT NOT NULL CHECK(verdict IN ('approved','edited','rejected')),
                source_opening TEXT NOT NULL,
                suggested_opening TEXT NOT NULL DEFAULT '',
                priority_assessment TEXT NOT NULL DEFAULT '' CHECK(priority_assessment IN (
                    '', 'accurate', 'too_high', 'too_low', 'not_suitable'
                )),
                priority_reason_code TEXT NOT NULL DEFAULT '',
                priority_note TEXT NOT NULL DEFAULT '',
                evidence_message_ref TEXT NOT NULL DEFAULT '',
                chat_snapshot_at TEXT NOT NULL DEFAULT '',
                revision_notes TEXT NOT NULL DEFAULT '',
                reviewer_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(sales_profile_id, reviewer_key)
            );
            CREATE INDEX IF NOT EXISTS sales_profile_opening_reviews_profile_time
                ON sales_profile_opening_reviews(sales_profile_id, updated_at DESC);
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(sales_profile_opening_reviews)"
            )
        }
        additions = (
            (
                "priority_assessment",
                "priority_assessment TEXT NOT NULL DEFAULT '' CHECK(priority_assessment IN "
                "('', 'accurate', 'too_high', 'too_low', 'not_suitable'))",
            ),
            ("priority_reason_code", "priority_reason_code TEXT NOT NULL DEFAULT ''"),
            ("priority_note", "priority_note TEXT NOT NULL DEFAULT ''"),
            ("evidence_message_ref", "evidence_message_ref TEXT NOT NULL DEFAULT ''"),
            ("chat_snapshot_at", "chat_snapshot_at TEXT NOT NULL DEFAULT ''"),
            ("revision_notes", "revision_notes TEXT NOT NULL DEFAULT ''"),
        )
        for column, definition in additions:
            if column not in columns:
                connection.execute(
                    "ALTER TABLE sales_profile_opening_reviews ADD COLUMN " + definition
                )
        connection.commit()
    finally:
        connection.close()


def _orders(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = facts.get("orders")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _effective_orders(facts: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in _orders(facts)
        if str(item.get("refund_type") or "").strip().lower() != "cancel"
    ]


def _association_anchor(sku_name: Any) -> Optional[Dict[str, Any]]:
    """Return only product-family matches we can explain without guessing."""
    name = re.sub(r"\s+", "", _clean_text(sku_name))
    if "拉夫" in name and "亚麻" in name and "衬衫" in name:
        return {
            "label": "拉夫劳伦亚麻衬衫",
            "terms": ("拉夫", "亚麻", "衬衫"),
        }
    return None


_PRODUCT_VARIANT_WORDS = re.compile(
    r"(?:黑色|白色|米白色|灰色|深灰色|浅灰色|藏蓝色|海军蓝|蓝色|浅蓝色|"
    r"绿色|红色|粉色|粉紫色|黄色|卡其色|棕色|紫色|橙色|驼色|银色|金色|"
    r"藏青色|米色|杏色|酒红色|咖色|墨绿色|军绿色|牛仔蓝)"
)
_PRODUCT_SIZE_WORDS = re.compile(
    r"(?:\b(?:XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|XXXXL)\b|\b\d{1,3}(?:\.\d)?码?\b|均码|无)",
    re.IGNORECASE,
)


def _product_family_name(sku_name: Any) -> str:
    name = _clean_text(sku_name).strip()
    if not name or "[" in name or "]" in name:
        return ""
    name = _PRODUCT_VARIANT_WORDS.sub(" ", name)
    name = _PRODUCT_SIZE_WORDS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_/·")
    return name if re.search(r"[\u4e00-\u9fff]", name) else ""


def _cross_sell_recommendations(
    conn: sqlite3.Connection,
    *,
    order_snapshot_id: Any,
    phone_hmac: Any,
) -> Dict[str, Any]:
    """Build conservative co-purchase evidence from the frozen order snapshot."""
    snapshot_id = str(order_snapshot_id or "")
    customer_phone = str(phone_hmac or "")
    empty = {
        "available": False,
        "anchor_product": "",
        "buyer_count": 0,
        "other_buyer_count": 0,
        "recommendations": [],
        "method_note": "当前订单里没有可稳定归类的同款商品，暂不生成连带购买结论。",
    }
    if not snapshot_id or not customer_phone:
        return empty
    anchor_rows = conn.execute(
        "SELECT sku_name FROM orders WHERE order_snapshot_id=? AND phone_hmac=? "
        "AND COALESCE(revenue_minor,0)>0 AND COALESCE(refund_type,'')='' "
        "AND COALESCE(return_status,'')='' AND COALESCE(sku_name,'')<>'' "
        "ORDER BY COALESCE(paid_at,paid_on) DESC,order_line_id DESC",
        (snapshot_id, customer_phone),
    ).fetchall()
    anchor = next(
        (
            candidate
            for candidate in (_association_anchor(row["sku_name"]) for row in anchor_rows)
            if candidate is not None
        ),
        None,
    )
    if anchor is None:
        return empty
    terms = tuple(str(term) for term in anchor["terms"])
    like_clause = " AND ".join("sku_name LIKE ?" for _ in terms)
    like_params = tuple("%%%s%%" % term for term in terms)
    buyer_rows = conn.execute(
        "SELECT DISTINCT phone_hmac FROM orders WHERE order_snapshot_id=? "
        "AND phone_hmac IS NOT NULL AND COALESCE(revenue_minor,0)>0 "
        "AND COALESCE(refund_type,'')='' AND COALESCE(return_status,'')='' AND "
        + like_clause,
        (snapshot_id, *like_params),
    ).fetchall()
    buyers = {str(row["phone_hmac"]) for row in buyer_rows if row["phone_hmac"]}
    other_buyers = buyers - {customer_phone}
    result: Dict[str, Any] = {
        "available": True,
        "anchor_product": anchor["label"],
        "buyer_count": len(buyers),
        "other_buyer_count": len(other_buyers),
        "recommendations": [],
        "method_note": "只统计当前冻结订单中无取消、退货或未结售后的有效购买；样本少时仅作参考。",
    }
    if not other_buyers:
        return result
    placeholders = ",".join("?" for _ in other_buyers)
    purchase_rows = conn.execute(
        "SELECT phone_hmac,sku_name FROM orders WHERE order_snapshot_id=? "
        "AND phone_hmac IN (" + placeholders + ") "
        "AND COALESCE(revenue_minor,0)>0 AND COALESCE(refund_type,'')='' "
        "AND COALESCE(return_status,'')='' AND COALESCE(sku_name,'')<>''",
        (snapshot_id, *sorted(other_buyers)),
    ).fetchall()
    supporters: Dict[str, set[str]] = {}
    occurrences: Counter[str] = Counter()
    for row in purchase_rows:
        raw_sku = str(row["sku_name"] or "")
        compact = re.sub(r"\s+", "", raw_sku)
        if all(term in compact for term in terms):
            continue
        family = _product_family_name(raw_sku)
        if len(family) < 2:
            continue
        supporters.setdefault(family, set()).add(str(row["phone_hmac"]))
        occurrences[family] += 1
    ranked = sorted(
        supporters,
        key=lambda family: (-len(supporters[family]), -occurrences[family], family),
    )[:5]
    result["recommendations"] = [
        {
            "product": family,
            "supporting_buyers": len(supporters[family]),
        }
        for family in ranked
    ]
    return result


def _iso_day(value: Any) -> Optional[datetime]:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _window_label(value: Any) -> str:
    window = _bounded_plain_text(value, limit=40)
    prefixes = {
        "09:00-12:00": "上午",
        "12:00-18:00": "下午",
        "18:00-24:00": "晚上",
    }
    return "%s %s" % (prefixes.get(window, "适合联系"), window.replace("-", "–", 1)) if window else ""


def _business_view(
    facts: Mapping[str, Any],
    *,
    contact_refusal: bool,
    future_signal: bool,
) -> Dict[str, Any]:
    features = facts.get("customer_features")
    if not isinstance(features, dict):
        features = {}
    orders = _orders(facts)
    cancelled_orders = [
        item
        for item in orders
        if str(item.get("refund_type") or "").strip().lower() == "cancel"
    ]
    effective_orders = _effective_orders(facts)
    paid_order_count = len(effective_orders)
    total_minor = sum(int(item.get("revenue_minor") or 0) for item in effective_orders)
    average_minor = round(total_minor / paid_order_count) if paid_order_count else 0
    cancelled_count = len(cancelled_orders)
    aftersales_orders = [
        item
        for item in effective_orders
        if str(item.get("refund_type") or "").strip().lower() in BUSINESS_AFTERSALES_TYPES
        and bool(item.get("refund_fact_at_cutoff", item.get("refund_on")))
    ]
    aftersales_count = len(aftersales_orders)
    aftersales_rate = aftersales_count / paid_order_count if paid_order_count else None
    unknown_aftersales_count = max(int(features.get("unknown_aftersales_count") or 0), 0)

    average_yuan = round(average_minor / 100, 2)
    total_yuan = round(total_minor / 100, 2)
    if average_yuan >= 350:
        average_score = 20
    elif average_yuan >= 280:
        average_score = 17
    elif average_yuan >= 220:
        average_score = 14
    elif average_yuan >= 160:
        average_score = 10
    else:
        average_score = 6
    if total_yuan >= 5000:
        spend_score = 15
    elif total_yuan >= 3000:
        spend_score = 12
    elif total_yuan >= 1500:
        spend_score = 9
    elif total_yuan >= 800:
        spend_score = 6
    else:
        spend_score = 3
    if paid_order_count >= 12:
        loyalty_score = 25
    elif paid_order_count >= 8:
        loyalty_score = 21
    elif paid_order_count >= 5:
        loyalty_score = 17
    elif paid_order_count >= 3:
        loyalty_score = 12
    elif paid_order_count >= 2:
        loyalty_score = 8
    else:
        loyalty_score = 4

    recency_days = features.get("rfm_recency_days")
    repurchase_days = features.get("median_repurchase_interval_days")
    paid_days = sorted(
        day
        for day in (
            _iso_day(item.get("paid_on") or item.get("paid_at"))
            for item in effective_orders
        )
        if day is not None
    )
    cutoff_day = _iso_day(facts.get("as_of_at"))
    if paid_days and cutoff_day is not None:
        recency_days = max((cutoff_day - paid_days[-1]).days, 0)
    if len(paid_days) >= 2:
        repurchase_days = median(
            (right - left).days for left, right in zip(paid_days, paid_days[1:])
        )
    try:
        recency = max(float(recency_days), 0.0) if recency_days is not None else None
    except (TypeError, ValueError):
        recency = None
    try:
        repurchase = max(float(repurchase_days), 1.0) if repurchase_days is not None else None
    except (TypeError, ValueError):
        repurchase = None
    if recency is not None and repurchase is not None:
        due_ratio = recency / repurchase
        if 0.8 <= due_ratio <= 1.5:
            timing_score = 15
        elif due_ratio > 1.5:
            timing_score = 13
        elif due_ratio >= 0.55:
            timing_score = 8
        else:
            timing_score = 3
    elif recency is not None and recency >= 60:
        timing_score = 10
    elif recency is not None and recency >= 30:
        timing_score = 7
    else:
        timing_score = 4
    if future_signal:
        timing_score = min(timing_score + 5, 20)

    if aftersales_rate is None:
        service_score = 8
    elif aftersales_rate == 0:
        service_score = 20
    elif aftersales_rate <= 0.10:
        service_score = 17
    elif aftersales_rate <= 0.20:
        service_score = 13
    elif aftersales_rate < 0.30:
        service_score = 8
    else:
        service_score = 0
    if unknown_aftersales_count:
        service_score = min(service_score, 8)
    priority_score = min(100, average_score + spend_score + loyalty_score + timing_score + service_score)

    exclusion_reasons = []
    if contact_refusal:
        exclusion_reasons.append("客户曾明确表示不希望联系")
    if aftersales_rate is not None and aftersales_rate >= 0.30:
        exclusion_reasons.append("历史售后率 %.0f%%，先服务、不促销" % (aftersales_rate * 100))
    if paid_order_count == 0:
        exclusion_reasons.append("没有有效付款记录")
    if exclusion_reasons:
        promotion_state = "excluded"
    elif unknown_aftersales_count:
        promotion_state = "review"
        exclusion_reasons.append(
            "有 %d 条售后事实待确认，确认前不进入促销列表" % unknown_aftersales_count
        )
    else:
        promotion_state = "eligible"
    promotion_eligible = promotion_state == "eligible"
    if promotion_state == "excluded":
        priority_label = "仅服务，不促销"
    elif promotion_state == "review":
        priority_label = "售后待确认"
    elif priority_score >= 80:
        priority_label = "优先跟进"
    elif priority_score >= 65:
        priority_label = "值得跟进"
    elif priority_score >= 50:
        priority_label = "常规跟进"
    else:
        priority_label = "低优先"

    rhythm = features.get("order_rhythm")
    if not isinstance(rhythm, dict):
        rhythm = {}
    preferred_period = str(rhythm.get("preferred_period") or "")
    order_habit = (
        "历史订单更常在%s完成" % PERIOD_LABELS[preferred_period]
        if rhythm.get("preference_state") == "supported" and preferred_period in PERIOD_LABELS
        else "暂无可信下单时段（历史订单只记录日期）"
    )
    contact_window = _window_label(features.get("recommended_contact_window"))
    contact_evidence = int(features.get("contact_window_evidence_count") or 0)
    contact_habit = contact_window if contact_window and contact_evidence >= 5 else "联系时段证据不足"
    paid_dates = [day.strftime("%Y-%m-%d") for day in paid_days]
    aftersales_text = "暂无售后" if not aftersales_count else "%.1f%%（%d / %d）" % (
        (aftersales_rate or 0) * 100,
        aftersales_count,
        paid_order_count,
    )
    if unknown_aftersales_count:
        aftersales_text += "，另有 %d 条待确认" % unknown_aftersales_count
    return {
        "priority_score": priority_score,
        "priority_label": priority_label,
        "promotion_eligible": promotion_eligible,
        "promotion_state": promotion_state,
        "exclusion_reason": "；".join(exclusion_reasons),
        "paid_order_count": paid_order_count,
        "repeat_count": max(paid_order_count - 1, 0),
        "historical_spend_yuan": total_yuan,
        "average_paid_amount_yuan": average_yuan,
        "aftersales_count": aftersales_count,
        "aftersales_rate_percent": round((aftersales_rate or 0) * 100, 1) if aftersales_rate is not None else None,
        "aftersales_summary": aftersales_text,
        "unknown_aftersales_count": unknown_aftersales_count,
        "cancelled_count": cancelled_count,
        "days_since_last_order": int(recency) if recency is not None else None,
        "median_repurchase_interval_days": round(repurchase, 1) if repurchase is not None else None,
        "last_order_date": paid_dates[-1] if paid_dates else None,
        "contact_habit": contact_habit,
        "order_habit": order_habit,
        "score_factors": [
            "客单均额 ¥%s" % ("%.2f" % average_yuan).rstrip("0").rstrip("."),
            "%d 条有效付款记录，%d 次复购" % (paid_order_count, max(paid_order_count - 1, 0)),
            "售后表现：%s" % aftersales_text,
        ],
    }


def _public_order(item: Mapping[str, Any]) -> Dict[str, Any]:
    refund_type = str(item.get("refund_type") or "").strip().lower()
    aftersales = AFTERSALES_LABELS.get(refund_type, "无售后")
    if refund_type in BUSINESS_AFTERSALES_TYPES and item.get("refund_amount_minor") is not None:
        aftersales += " · ¥%.2f" % (int(item.get("refund_amount_minor") or 0) / 100)
    sku_name = _bounded_plain_text(item.get("sku_name"), limit=120)
    product_parts = [sku_name]
    for detail in (
        _bounded_plain_text(item.get("color"), limit=40),
        _bounded_plain_text(item.get("size"), limit=40),
    ):
        if not detail:
            continue
        if detail.isascii() and detail.isalnum():
            already_in_name = bool(
                re.search(
                    r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(detail),
                    sku_name,
                    flags=re.IGNORECASE,
                )
            )
        else:
            already_in_name = detail.casefold() in sku_name.casefold()
        if not already_in_name:
            product_parts.append(detail)
    return {
        "paid_on": _bounded_plain_text(item.get("paid_on") or item.get("paid_at"), limit=32)[:10],
        "product": " · ".join(part for part in product_parts if part) or "商品信息待补全",
        "channel": _bounded_plain_text(item.get("platform"), limit=80) or "渠道待补全",
        "amount_yuan": round(int(item.get("revenue_minor") or 0) / 100, 2),
        "aftersales": aftersales,
        "status": "已取消" if refund_type == "cancel" else ("有售后" if refund_type in BUSINESS_AFTERSALES_TYPES else "已付款"),
    }


def _public_customer(
    row: Mapping[str, Any],
    facts: Mapping[str, Any],
    customer_index: Mapping[str, Mapping[str, str]],
) -> Dict[str, Any]:
    identity = customer_index.get(str(row.get("phone_hmac") or ""), {})
    name = _bounded_plain_text(identity.get("name") or row.get("display_name"), limit=80) or "客户"
    phone = normalize_phone(str(identity.get("phone") or "")) or ""
    member_facts = facts.get("member_facts")
    member_shop = ""
    if isinstance(member_facts, list):
        for item in member_facts:
            if isinstance(item, dict) and item.get("member_shop"):
                member_shop = _bounded_plain_text(item.get("member_shop"), limit=80)
                break
    orders = sorted(_orders(facts), key=lambda item: str(item.get("paid_on") or ""))
    last_channel = _bounded_plain_text(orders[-1].get("platform"), limit=80) if orders else ""
    return {
        "name": name,
        "phone": phone or "手机号待补全",
        "phone_hint": _mask_phone(phone),
        "member_shop": member_shop or "会员归属店铺待补全",
        "last_order_channel": last_channel or "成交渠道待补全",
    }


def _public_facts(facts: Mapping[str, Any], business: Mapping[str, Any]) -> Dict[str, Any]:
    features = facts.get("customer_features") if isinstance(facts, dict) else {}
    if not isinstance(features, dict):
        features = {}
    member_facts = facts.get("member_facts") if isinstance(facts, dict) else []
    effective_orders = _effective_orders(facts)

    def preferences(field: str, limit: int) -> list[str]:
        values = [
            _bounded_plain_text(item.get(field), limit=120)
            for item in effective_orders
        ]
        counts = Counter(value for value in values if value)
        return [value for value, _ in counts.most_common(limit)]

    return _clean_value(
        {
            "value_level": features.get("value_bucket"),
            "historical_orders": business.get("paid_order_count"),
            "historical_spend_yuan": business.get("historical_spend_yuan"),
            "average_paid_amount_yuan": business.get("average_paid_amount_yuan"),
            "days_since_last_order": business.get("days_since_last_order"),
            "contact_habit": business.get("contact_habit"),
            "order_habit": business.get("order_habit"),
            "preferred_products": preferences("sku_name", 5),
            "preferred_colors": preferences("color", 3),
            "preferred_sizes": preferences("size", 3),
            "member_profile_matched": bool(member_facts),
            "inventory_assumption": "默认满库存，可按历史偏好推荐商品",
        }
    )


def _public_event(row: Mapping[str, Any]) -> Dict[str, Any]:
    event = _json(row.get("event_json"), {})
    evidence = _json(row.get("evidence_json"), [])
    public_evidence = []
    for index, item in enumerate(evidence if isinstance(evidence, list) else [], 1):
        if not isinstance(item, dict):
            continue
        quote = item.get("quote") or item.get("excerpt") or item.get("text")
        if not quote and item.get("kind") == "order" and item.get("field"):
            field = str(item.get("field"))
            value = item.get("value")
            if isinstance(value, bool):
                value = "是" if value else "否"
            if field == "refund_type":
                value = AFTERSALES_LABELS.get(str(value or "").lower(), "售后记录")
            quote = "%s：%s" % (EVIDENCE_FIELD_LABELS.get(field, "订单事实"), value)
        if not quote:
            continue
        public_evidence.append(
            {
                "label": "聊天证据 %02d" % index if item.get("kind") == "message" else "订单证据 %02d" % index,
                "quote": _clean_text(quote),
            }
        )
    return {
        "label": EVENT_LABELS.get(str(row.get("event_type") or ""), "销售线索"),
        "summary": _clean_text(event.get("summary") if isinstance(event, dict) else ""),
        "evidence": public_evidence,
    }


def _public_review(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "review_id": row.get("review_id"),
        "card_version": row.get("card_version"),
        "verdict": row.get("verdict"),
        "source_opening": _clean_text(row.get("source_opening")),
        "suggested_opening": _clean_text(row.get("suggested_opening")),
        "priority_assessment": row.get("priority_assessment") or "",
        "priority_reason_code": row.get("priority_reason_code") or "",
        "priority_note": _clean_text(row.get("priority_note")),
        "evidence_message_ref": row.get("evidence_message_ref") or "",
        "chat_snapshot_at": row.get("chat_snapshot_at") or "",
        "revision_notes": _clean_text(row.get("revision_notes")),
        "updated_at": row.get("updated_at"),
    }


def _opening_for_storage(value: Any, *, customer_name: str = "") -> str:
    text = str(value or "")
    text = SEPARATED_PHONE.sub("[手机号已隐藏]", text)
    text = SEPARATED_IDENTITY.sub("[身份信息已隐藏]", text)
    text = _clean_text(text).strip()
    name = _bounded_plain_text(customer_name, limit=80)
    if len(name) >= 2:
        flexible_name = r"[\s·•._-]*".join(re.escape(character) for character in name)
        text = re.sub(flexible_name, "[客户称呼]", text, flags=re.IGNORECASE)
    elif name:
        text = re.sub(
            re.escape(name) + r"(?=(?:姐|哥|总|老师|女士|先生|您好|你好))",
            "[客户称呼]",
            text,
        )
    return text


def _same_opening(left: str, right: str) -> bool:
    normalize = lambda value: re.sub(r"[\s，。！？、,.!?；;：:]", "", value or "")
    return bool(normalize(left)) and normalize(left) == normalize(right)


def _validate_review(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_review", "审核内容格式错误")
    fields = set(body)
    allowed = REVIEW_REQUIRED_FIELDS | frozenset(REVIEW_OPTIONAL_FIELDS)
    if not REVIEW_REQUIRED_FIELDS.issubset(fields) or not fields.issubset(allowed):
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_review", "审核字段不完整")
    card_version = str(body.get("card_version") or "").strip()
    verdict = str(body.get("verdict") or "").strip()
    suggested_opening = str(body.get("suggested_opening") or "").strip()
    if not card_version or len(card_version) > 256:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_card_version", "卡片版本无效")
    if verdict not in {"approved", "edited", "rejected"}:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_verdict", "请选择审核结论")
    if len(suggested_opening) > 2000 or any(
        ord(char) < 32 and char not in "\n\r\t" for char in suggested_opening
    ):
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_opening_suggestion", "开场建议格式错误")
    if verdict in {"edited", "rejected"} and not suggested_opening:
        raise PortalError(
            HTTPStatus.BAD_REQUEST,
            "missing_opening_suggestion",
            "选择该结论时，请写出更合适的开场",
        )
    optional_values: Dict[str, str] = {}
    for field in REVIEW_OPTIONAL_FIELDS:
        if field not in body:
            continue
        value = str(body.get(field) or "").strip()
        limit = 2000 if field == "revision_notes" else (100 if field == "priority_note" else 256)
        if len(value) > limit or any(
            ord(char) < 32 and char not in "\n\r\t" for char in value
        ):
            raise PortalError(
                HTTPStatus.BAD_REQUEST,
                "invalid_review_feedback",
                "修改意见格式错误",
            )
        optional_values[field] = value
    assessment = optional_values.get("priority_assessment")
    if assessment is not None and assessment not in PRIORITY_ASSESSMENTS:
        raise PortalError(
            HTTPStatus.BAD_REQUEST,
            "invalid_priority_assessment",
            "请选择有效的优先级判断",
        )
    return {
        "card_version": card_version,
        "verdict": verdict,
        "suggested_opening": suggested_opening,
        "optional_values": optional_values,
    }


def _cursor_context(
    run_id: str,
    profile_id: str,
    as_of_at: str,
    record_count: int,
) -> bytes:
    return "\0".join(
        (run_id, profile_id, as_of_at, str(record_count))
    ).encode("utf-8")


def _encode_message_cursor(
    message_key: str,
    *,
    key: bytes,
    run_id: str,
    profile_id: str,
    as_of_at: str,
    record_count: int,
) -> str:
    encoded_ref = base64.urlsafe_b64encode(message_key.encode("utf-8")).rstrip(b"=")
    signature = hmac.new(
        key,
        _cursor_context(run_id, profile_id, as_of_at, record_count) + b"\0" + encoded_ref,
        hashlib.sha256,
    ).digest()[:16]
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return "v1.%s.%s" % (encoded_ref.decode("ascii"), encoded_signature.decode("ascii"))


def _decode_message_cursor(
    cursor: str,
    *,
    key: bytes,
    run_id: str,
    profile_id: str,
    as_of_at: str,
    record_count: int,
) -> str:
    try:
        version, encoded_ref_text, encoded_signature_text = cursor.split(".")
        if version != "v1" or not encoded_ref_text or not encoded_signature_text:
            raise ValueError
        encoded_ref = encoded_ref_text.encode("ascii")
        signature = base64.urlsafe_b64decode(
            encoded_signature_text + "=" * (-len(encoded_signature_text) % 4)
        )
        expected = hmac.new(
            key,
            _cursor_context(run_id, profile_id, as_of_at, record_count) + b"\0" + encoded_ref,
            hashlib.sha256,
        ).digest()[:16]
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        message_key = base64.urlsafe_b64decode(
            encoded_ref_text + "=" * (-len(encoded_ref_text) % 4)
        ).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise PortalError(
            HTTPStatus.BAD_REQUEST,
            "invalid_message_cursor",
            "聊天记录游标无效",
        ) from exc
    if not message_key or len(message_key) > 512 or any(ord(char) < 33 for char in message_key):
        raise PortalError(
            HTTPStatus.BAD_REQUEST,
            "invalid_message_cursor",
            "聊天记录游标无效",
        )
    return message_key


class ReviewPortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        *,
        db_path: Path,
        run_id: str,
        allowed_hosts: Sequence[str],
        customer_index: Mapping[str, Mapping[str, str]],
        customer_source_synced_at: Optional[str],
    ) -> None:
        super().__init__(address, ReviewPortalHandler)
        self.db_path = db_path
        self.run_id = run_id
        self.allowed_hosts = {item.strip().lower() for item in allowed_hosts if item.strip()}
        self.customer_index = {
            str(key): dict(value) for key, value in customer_index.items()
        }
        self.customer_source_synced_at = customer_source_synced_at
        self.cursor_key = hashlib.sha256(
            ("review-portal-message-cursor\0" + str(db_path)).encode("utf-8")
        ).digest()
        self.write_lock = threading.Lock()


class ReviewPortalHandler(BaseHTTPRequestHandler):
    server: ReviewPortalServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        path = urlsplit(getattr(self, "path", "/")).path
        print("review-portal method=%s path=%s status=%s" % (self.command, path, getattr(self, "_status", "-")))

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            self._check_host()
            if not path.startswith("/api/"):
                if method != "GET":
                    raise PortalError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "不支持该操作")
                self._static(path)
                return
            if method == "GET" and path == "/api/summary":
                self._summary()
            elif method == "GET" and path == "/api/profiles":
                self._profiles(parse_qs(split.query))
            elif method == "GET" and path.startswith("/api/profiles/") and path.endswith("/messages"):
                profile_id = path[len("/api/profiles/") : -len("/messages")].strip("/")
                self._messages(
                    profile_id,
                    parse_qs(split.query, keep_blank_values=True),
                )
            elif method == "GET" and path.startswith("/api/profiles/"):
                self._profile(path[len("/api/profiles/") :].strip("/"))
            elif method == "POST" and path.startswith("/api/profiles/") and path.endswith("/review"):
                profile_id = path[len("/api/profiles/") : -len("/review")].strip("/")
                self._save_review(profile_id)
            else:
                raise PortalError(HTTPStatus.NOT_FOUND, "not_found", "页面接口不存在")
        except PortalError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            self._error(PortalError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "服务暂时不可用"))

    def _check_host(self) -> None:
        host = self.headers.get("Host", "").strip().lower()
        if host.startswith("["):
            hostname = host.split("]", 1)[0].lstrip("[")
        else:
            hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if not hostname or hostname not in self.server.allowed_hosts:
            raise PortalError(HTTPStatus.FORBIDDEN, "host_denied", "访问地址未获允许")
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and origin not in {"http://" + host, "https://" + host}:
            raise PortalError(HTTPStatus.FORBIDDEN, "origin_denied", "跨站请求已拒绝")

    def _db(self, *, write: bool = False) -> sqlite3.Connection:
        if not self.server.db_path.is_file():
            raise PortalError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "画像数据尚未准备好")
        conn = sqlite3.connect(str(self.server.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not write:
            conn.execute("PRAGMA query_only = ON")
        return conn

    def _run(self, conn: sqlite3.Connection) -> sqlite3.Row:
        fields = "sales_profile_run_id,as_of_at,status,model,created_at,completed_at"
        if self.server.run_id == "latest":
            row = conn.execute(
                "SELECT %s FROM sales_profile_runs ORDER BY created_at DESC,sales_profile_run_id DESC LIMIT 1" % fields
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT %s FROM sales_profile_runs WHERE sales_profile_run_id=?" % fields,
                (self.server.run_id,),
            ).fetchone()
        if row is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "run_not_found", "画像批次不存在")
        return row

    @staticmethod
    def _profile_id(value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_profile", "画像编号无效")
        return value

    def _profile_message_context(
        self,
        conn: sqlite3.Connection,
        profile_id: str,
        run: Mapping[str, Any],
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT p.card_version,p.profile_json,s.phone_hmac,s.customer_key,"
            "r.as_of_at,r.message_snapshot_id,ss.record_count "
            "FROM sales_profiles p JOIN sales_profile_subjects s ON s.subject_id=p.subject_id "
            "JOIN sales_profile_runs r ON r.sales_profile_run_id=s.sales_profile_run_id "
            "JOIN source_snapshots ss ON ss.snapshot_id=r.message_snapshot_id "
            " AND ss.run_id=r.source_run_id "
            "WHERE p.sales_profile_id=? AND s.sales_profile_run_id=? AND p.status='succeeded'",
            (profile_id, run["sales_profile_run_id"]),
        ).fetchone()
        if row is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "profile_not_found", "画像不存在")
        record_count = row["record_count"]
        if record_count is None or int(record_count) < 0:
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "message_snapshot_unavailable",
                "冻结聊天记录暂不可用",
            )
        return row

    def _messages(
        self,
        profile_id: str,
        query: Mapping[str, Sequence[str]],
    ) -> None:
        profile_id = self._profile_id(profile_id)
        raw_limit = (query.get("limit") or [str(DEFAULT_MESSAGE_LIMIT)])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise PortalError(
                HTTPStatus.BAD_REQUEST,
                "invalid_message_limit",
                "聊天记录数量无效",
            ) from exc
        if limit < 1 or limit > MAX_MESSAGE_LIMIT:
            raise PortalError(
                HTTPStatus.BAD_REQUEST,
                "invalid_message_limit",
                "聊天记录数量需在 1 到 %d 之间" % MAX_MESSAGE_LIMIT,
            )
        before = (query.get("before") or [""])[0].strip()
        with self._db() as conn:
            run = self._run(conn)
            context = self._profile_message_context(conn, profile_id, run)
            run_id = str(run["sales_profile_run_id"])
            customer_key = str(context["customer_key"])
            as_of_at = str(context["as_of_at"])
            record_count = int(context["record_count"])
            aggregate = conn.execute(
                "SELECT COUNT(*) total,MAX(timestamp) snapshot_at FROM messages "
                "WHERE customer_key=? AND timestamp<=? AND source_ordinal<=?",
                (customer_key, as_of_at, record_count),
            ).fetchone()
            cursor_order: Optional[Tuple[str, int, str]] = None
            if before:
                cursor_message_key = _decode_message_cursor(
                    before,
                    key=self.server.cursor_key,
                    run_id=run_id,
                    profile_id=profile_id,
                    as_of_at=as_of_at,
                    record_count=record_count,
                )
                cursor_row = conn.execute(
                    "SELECT timestamp,source_ordinal,message_key FROM messages "
                    "WHERE message_key=? AND customer_key=? AND timestamp<=? AND source_ordinal<=?",
                    (cursor_message_key, customer_key, as_of_at, record_count),
                ).fetchone()
                if cursor_row is None:
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_message_cursor",
                        "聊天记录游标无效",
                    )
                cursor_order = (
                    str(cursor_row["timestamp"]),
                    int(cursor_row["source_ordinal"]),
                    str(cursor_row["message_key"]),
                )
            clauses = [
                "customer_key=?",
                "timestamp<=?",
                "source_ordinal<=?",
            ]
            params: list[Any] = [customer_key, as_of_at, record_count]
            if cursor_order is not None:
                timestamp, ordinal, message_key = cursor_order
                clauses.append(
                    "(timestamp<? OR (timestamp=? AND source_ordinal<?) OR "
                    "(timestamp=? AND source_ordinal=? AND message_key<?))"
                )
                params.extend(
                    (timestamp, timestamp, ordinal, timestamp, ordinal, message_key)
                )
            params.append(limit + 1)
            rows = conn.execute(
                "SELECT message_key,role,timestamp,text,source_ordinal FROM messages WHERE "
                + " AND ".join(clauses)
                + " ORDER BY timestamp DESC,source_ordinal DESC,message_key DESC LIMIT ?",
                params,
            ).fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = _encode_message_cursor(
                str(page_rows[-1]["message_key"]),
                key=self.server.cursor_key,
                run_id=run_id,
                profile_id=profile_id,
                as_of_at=as_of_at,
                record_count=record_count,
            )
        items = [
            {
                "message_ref": row["message_key"],
                "role": row["role"],
                "timestamp": row["timestamp"],
                "text": _clean_text(row["text"]),
            }
            for row in reversed(page_rows)
        ]
        self._send_json(
            HTTPStatus.OK,
            {
                "items": items,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "total": int(aggregate["total"] or 0),
                "snapshot_at": aggregate["snapshot_at"] or "",
            },
        )

    def _listing_rows(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        stratum: str = "",
    ) -> list[Dict[str, Any]]:
        clauses = ["s.sales_profile_run_id=?", "p.status='succeeded'"]
        params: list[Any] = [run_id]
        if stratum:
            clauses.append("s.stratum=?")
            params.append(stratum)
        rows = conn.execute(
            "SELECT p.sales_profile_id,p.card_version,p.deterministic_facts_json,"
            "s.stratum,s.stratum_rank,s.phone_hmac,s.profile_id,c.display_name,"
            "(SELECT COUNT(*) FROM sales_profile_opening_reviews rv "
            " WHERE rv.sales_profile_id=p.sales_profile_id AND rv.reviewer_key=?) review_count,"
            "(SELECT verdict FROM sales_profile_opening_reviews rv "
            " WHERE rv.sales_profile_id=p.sales_profile_id AND rv.reviewer_key=? "
            " ORDER BY updated_at DESC,review_id DESC LIMIT 1) latest_verdict,"
            "EXISTS(SELECT 1 FROM sales_profile_events e WHERE e.subject_id=s.subject_id "
            " AND e.validation_state='accepted' AND e.event_type='contact_refusal') contact_refusal,"
            "EXISTS(SELECT 1 FROM sales_profile_events e WHERE e.subject_id=s.subject_id "
            " AND e.validation_state='accepted' AND e.event_type IN "
            " ('future_return','delayed_purchase','stock_wait','promotion_or_payday_wait')) future_signal "
            "FROM sales_profile_subjects s JOIN sales_profiles p ON p.subject_id=s.subject_id "
            "JOIN customers c ON c.customer_key=s.customer_key WHERE "
            + " AND ".join(clauses),
            (SHARED_REVIEWER_KEY, SHARED_REVIEWER_KEY, *params),
        ).fetchall()
        items = []
        for raw_row in rows:
            row = dict(raw_row)
            facts = _json(row.get("deterministic_facts_json"), {})
            if not isinstance(facts, dict):
                facts = {}
            business = _business_view(
                facts,
                contact_refusal=bool(row.get("contact_refusal")),
                future_signal=bool(row.get("future_signal")),
            )
            customer = _public_customer(row, facts, self.server.customer_index)
            items.append(
                {
                    "sales_profile_id": row["sales_profile_id"],
                    "label": customer["name"],
                    "phone_hint": customer["phone_hint"],
                    "stratum": row["stratum"],
                    "rank": int(row["stratum_rank"]),
                    "review_count": int(row.get("review_count") or 0),
                    "latest_verdict": row.get("latest_verdict"),
                    "card_version": row["card_version"],
                    "priority_score": business["priority_score"],
                    "priority_label": business["priority_label"],
                    "promotion_eligible": business["promotion_eligible"],
                    "promotion_state": business["promotion_state"],
                    "exclusion_reason": business["exclusion_reason"],
                    "paid_order_count": business["paid_order_count"],
                    "average_paid_amount_yuan": business["average_paid_amount_yuan"],
                    "days_since_last_order": business["days_since_last_order"],
                }
            )
        items.sort(
            key=lambda item: (
                {"eligible": 0, "review": 1, "excluded": 2}.get(
                    str(item.get("promotion_state") or ""), 3
                ),
                -int(item["priority_score"]),
                item["days_since_last_order"] if item["days_since_last_order"] is not None else 999999,
                str(item["label"]),
            )
        )
        return items

    def _summary(self) -> None:
        with self._db() as conn:
            run = self._run(conn)
            run_id = run["sales_profile_run_id"]
            items = self._listing_rows(conn, run_id)
            verdicts = {
                row["verdict"]: int(row["n"])
                for row in conn.execute(
                    "SELECT verdict,COUNT(DISTINCT sales_profile_id) n "
                    "FROM sales_profile_opening_reviews rv "
                    "WHERE rv.reviewer_key=? AND EXISTS(SELECT 1 FROM sales_profile_subjects s "
                    "JOIN sales_profiles p ON p.subject_id=s.subject_id "
                    "WHERE p.sales_profile_id=rv.sales_profile_id AND s.sales_profile_run_id=?) "
                    "GROUP BY verdict",
                    (SHARED_REVIEWER_KEY, run_id),
                )
            }
        reviewed = sum(bool(item["review_count"]) for item in items)
        self._send_json(
            HTTPStatus.OK,
            {
                "run": dict(run),
                "total": len(items),
                "generated": len(items),
                "reviewed": reviewed,
                "promotion_eligible": sum(bool(item["promotion_eligible"]) for item in items),
                "promotion_review": sum(item["promotion_state"] == "review" for item in items),
                "promotion_excluded": sum(item["promotion_state"] == "excluded" for item in items),
                "verdicts": verdicts,
                "inventory_assumption": "默认满库存",
                "customer_source_synced_at": self.server.customer_source_synced_at,
                "send_allowed": False,
            },
        )

    def _profiles(self, query: Mapping[str, Sequence[str]]) -> None:
        stratum = (query.get("stratum") or [""])[0]
        status = (query.get("status") or [""])[0]
        promotion = (query.get("promotion") or ["eligible"])[0] or "eligible"
        if stratum and stratum not in STRATUM_LABELS:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_stratum", "客户分层无效")
        if status not in {"", "reviewed", "unreviewed"}:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_status", "审核状态无效")
        if promotion not in {"eligible", "review", "excluded", "all"}:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_promotion", "促销状态无效")
        with self._db() as conn:
            run = self._run(conn)
            items = self._listing_rows(conn, run["sales_profile_run_id"], stratum=stratum)
        if status == "reviewed":
            items = [item for item in items if item["review_count"]]
        elif status == "unreviewed":
            items = [item for item in items if not item["review_count"]]
        if promotion == "eligible":
            items = [item for item in items if item["promotion_eligible"]]
        elif promotion == "review":
            items = [item for item in items if item["promotion_state"] == "review"]
        elif promotion == "excluded":
            items = [item for item in items if item["promotion_state"] == "excluded"]
        self._send_json(
            HTTPStatus.OK,
            {
                "items": items,
                "total": len(items),
                "run_id": run["sales_profile_run_id"],
                "promotion_filter": promotion,
                "send_allowed": False,
            },
        )

    def _profile(self, profile_id: str) -> None:
        profile_id = self._profile_id(profile_id)
        with self._db() as conn:
            run = self._run(conn)
            row = conn.execute(
                "SELECT p.sales_profile_id,p.card_version,p.profile_json,p.deterministic_facts_json,"
                "p.model,p.updated_at,s.stratum,s.stratum_rank,s.phone_hmac,s.profile_id,"
                "c.display_name,r.as_of_at,r.order_snapshot_id "
                "FROM sales_profiles p JOIN sales_profile_subjects s ON s.subject_id=p.subject_id "
                "JOIN customers c ON c.customer_key=s.customer_key "
                "JOIN sales_profile_runs r ON r.sales_profile_run_id=s.sales_profile_run_id "
                "WHERE p.sales_profile_id=? AND s.sales_profile_run_id=? AND p.status='succeeded'",
                (profile_id, run["sales_profile_run_id"]),
            ).fetchone()
            if row is None:
                raise PortalError(HTTPStatus.NOT_FOUND, "profile_not_found", "画像不存在")
            event_rows = conn.execute(
                "SELECT e.event_type,e.event_json,e.evidence_json FROM sales_profile_events e "
                "JOIN sales_profile_subjects s ON s.subject_id=e.subject_id "
                "JOIN sales_profiles p ON p.subject_id=s.subject_id "
                "WHERE p.sales_profile_id=? AND e.validation_state='accepted' "
                "ORDER BY e.chunk_index,e.created_at,e.sales_profile_event_id",
                (profile_id,),
            ).fetchall()
            review_rows = conn.execute(
                "SELECT review_id,card_version,verdict,source_opening,suggested_opening,"
                "priority_assessment,priority_reason_code,priority_note,evidence_message_ref,"
                "chat_snapshot_at,revision_notes,updated_at "
                "FROM sales_profile_opening_reviews WHERE sales_profile_id=? AND reviewer_key=? "
                "ORDER BY updated_at DESC,review_id DESC",
                (profile_id, SHARED_REVIEWER_KEY),
            ).fetchall()
            cross_sell = _cross_sell_recommendations(
                conn,
                order_snapshot_id=row["order_snapshot_id"],
                phone_hmac=row["phone_hmac"],
            )
        row_dict = dict(row)
        facts = _json(row_dict.get("deterministic_facts_json"), {})
        if not isinstance(facts, dict):
            facts = {}
        event_dicts = [dict(item) for item in event_rows]
        event_types = {str(item.get("event_type") or "") for item in event_dicts}
        business = _business_view(
            facts,
            contact_refusal="contact_refusal" in event_types,
            future_signal=bool(event_types & FUTURE_EVENT_TYPES),
        )
        customer = _public_customer(row_dict, facts, self.server.customer_index)
        order_history = [
            _public_order(item)
            for item in sorted(
                _orders(facts),
                key=lambda item: str(item.get("paid_on") or item.get("paid_at") or ""),
                reverse=True,
            )
        ]
        self._send_json(
            HTTPStatus.OK,
            {
                "sales_profile_id": row["sales_profile_id"],
                "label": customer["name"],
                "stratum": row["stratum"],
                "rank": int(row["stratum_rank"]),
                "card_version": row["card_version"],
                "as_of_at": row["as_of_at"],
                "customer": customer,
                "business": business,
                "card": _clean_value(_json(row["profile_json"], {})),
                "facts": _public_facts(facts, business),
                "order_history": order_history,
                "events": [_public_event(item) for item in event_dicts],
                "reviews": [_public_review(dict(item)) for item in review_rows],
                "cross_sell": cross_sell,
                "inventory_assumption": "默认满库存",
                "send_allowed": False,
            },
        )

    def _save_review(self, profile_id: str) -> None:
        profile_id = self._profile_id(profile_id)
        body = _validate_review(self._read_json())
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        review_id = "sales_profile_opening_review_" + hashlib.sha256(
            (profile_id + "\0" + SHARED_REVIEWER_KEY).encode("utf-8")
        ).hexdigest()[:24]
        with self.server.write_lock:
            with self._db(write=True) as conn:
                run = self._run(conn)
                row = self._profile_message_context(conn, profile_id, run)
                current_version = str(row["card_version"] or "")
                if not hmac.compare_digest(current_version, body["card_version"]):
                    raise PortalError(HTTPStatus.CONFLICT, "card_version_conflict", "卡片已更新，请刷新后重新审核")
                existing = conn.execute(
                    "SELECT card_version,priority_assessment,priority_reason_code,priority_note,"
                    "evidence_message_ref,revision_notes FROM sales_profile_opening_reviews "
                    "WHERE sales_profile_id=? AND reviewer_key=?",
                    (profile_id, SHARED_REVIEWER_KEY),
                ).fetchone()
                same_review_version = bool(
                    existing is not None
                    and hmac.compare_digest(
                        str(existing["card_version"] or ""), current_version
                    )
                )
                optional_values = body["optional_values"]
                priority_feedback: Dict[str, str] = {}
                for field in REVIEW_OPTIONAL_FIELDS:
                    if field in optional_values:
                        priority_feedback[field] = optional_values[field]
                    elif same_review_version:
                        priority_feedback[field] = str(existing[field] or "")
                    else:
                        priority_feedback[field] = ""
                if (
                    "priority_assessment" in optional_values
                    and "priority_reason_code" not in optional_values
                    and existing is not None
                    and optional_values["priority_assessment"]
                    != str(existing["priority_assessment"] or "")
                ):
                    priority_feedback["priority_reason_code"] = ""
                priority_assessment = priority_feedback["priority_assessment"]
                priority_reason_code = priority_feedback["priority_reason_code"]
                if priority_assessment not in PRIORITY_ASSESSMENTS:
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_priority_assessment",
                        "请选择有效的优先级判断",
                    )
                if priority_reason_code not in PRIORITY_REASON_CODES:
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_priority_reason_code",
                        "请选择有效的优先级原因",
                    )
                if priority_assessment and priority_assessment != "accurate" and not priority_reason_code:
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST,
                        "missing_priority_reason",
                        "调整优先级时，请选择原因",
                    )
                card = _json(row["profile_json"], {})
                identity = self.server.customer_index.get(str(row["phone_hmac"] or ""), {})
                customer_name = str(identity.get("name") or "")
                raw_source_opening = (
                    str(card.get("natural_opening") or "")
                    if isinstance(card, dict)
                    else ""
                )
                source_opening = _opening_for_storage(
                    raw_source_opening,
                    customer_name=customer_name,
                )[:2000]
                suggested_opening = _opening_for_storage(
                    body["suggested_opening"],
                    customer_name=customer_name,
                )[:2000]
                priority_note = _opening_for_storage(
                    priority_feedback["priority_note"],
                    customer_name=customer_name,
                )[:100]
                revision_notes = _opening_for_storage(
                    priority_feedback["revision_notes"],
                    customer_name=customer_name,
                )[:2000]
                if body["verdict"] == "approved":
                    suggested_opening = ""
                if body["verdict"] in {"edited", "rejected"} and not suggested_opening:
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST,
                        "missing_opening_suggestion",
                        "选择该结论时，请写出更合适的开场",
                    )
                if body["verdict"] in {"edited", "rejected"} and _same_opening(
                    source_opening, suggested_opening
                ):
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST,
                        "unchanged_opening_suggestion",
                        "修改后的开场不能与原开场相同",
                    )
                as_of_at = str(row["as_of_at"])
                record_count = int(row["record_count"])
                customer_key = str(row["customer_key"])
                evidence_message_ref = priority_feedback["evidence_message_ref"]
                if evidence_message_ref:
                    evidence = conn.execute(
                        "SELECT 1 FROM messages WHERE message_key=? AND customer_key=? "
                        "AND role='customer' AND timestamp<=? AND source_ordinal<=?",
                        (
                            evidence_message_ref,
                            customer_key,
                            as_of_at,
                            record_count,
                        ),
                    ).fetchone()
                    if evidence is None:
                        raise PortalError(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_evidence_message",
                            "请选择该客户冻结聊天中的客户消息",
                        )
                chat_snapshot_at = conn.execute(
                    "SELECT MAX(timestamp) FROM messages WHERE customer_key=? "
                    "AND timestamp<=? AND source_ordinal<=?",
                    (customer_key, as_of_at, record_count),
                ).fetchone()[0] or ""
                conn.execute(
                    "INSERT INTO sales_profile_opening_reviews(review_id,sales_profile_id,card_version,verdict,"
                    "source_opening,suggested_opening,priority_assessment,priority_reason_code,"
                    "priority_note,evidence_message_ref,chat_snapshot_at,revision_notes,reviewer_key,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sales_profile_id,reviewer_key) DO UPDATE SET "
                    "card_version=excluded.card_version,verdict=excluded.verdict,"
                    "source_opening=excluded.source_opening,suggested_opening=excluded.suggested_opening,"
                    "priority_assessment=excluded.priority_assessment,"
                    "priority_reason_code=excluded.priority_reason_code,"
                    "priority_note=excluded.priority_note,"
                    "evidence_message_ref=excluded.evidence_message_ref,"
                    "chat_snapshot_at=excluded.chat_snapshot_at,"
                    "revision_notes=excluded.revision_notes,"
                    "updated_at=excluded.updated_at",
                    (
                        review_id,
                        profile_id,
                        current_version,
                        body["verdict"],
                        source_opening,
                        suggested_opening,
                        priority_assessment,
                        priority_reason_code,
                        priority_note,
                        evidence_message_ref,
                        chat_snapshot_at,
                        revision_notes,
                        SHARED_REVIEWER_KEY,
                        now,
                        now,
                    ),
                )
                conn.commit()
                stored = conn.execute(
                    "SELECT review_id,card_version,verdict,source_opening,suggested_opening,"
                    "priority_assessment,priority_reason_code,priority_note,evidence_message_ref,"
                    "chat_snapshot_at,revision_notes,updated_at "
                    "FROM sales_profile_opening_reviews WHERE sales_profile_id=? AND reviewer_key=?",
                    (profile_id, SHARED_REVIEWER_KEY),
                ).fetchone()
        self._send_json(HTTPStatus.OK, {"review": _public_review(dict(stored)), "send_allowed": False})

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_body", "请求内容格式错误") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "请求内容过大")
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise PortalError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "仅接受 JSON")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求内容不是有效 JSON") from exc

    def _headers(self, *, api: bool) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store" if api else "no-cache")

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._status = status
        self.send_response(status)
        self._headers(api=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: PortalError) -> None:
        self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

    def _static(self, path: str) -> None:
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        item = files.get(path)
        if not item:
            raise PortalError(HTTPStatus.NOT_FOUND, "not_found", "页面不存在")
        body = (STATIC_DIR / item[0]).read_bytes()
        self._status = HTTPStatus.OK
        self.send_response(HTTPStatus.OK)
        self._headers(api=False)
        self.send_header("Content-Type", item[1])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    *,
    db_path: Path,
    run_id: str = "latest",
    allowed_hosts: Sequence[str] = (),
    customer_data_path: Optional[Path] = None,
    hmac_secret_path: Optional[Path] = None,
) -> ReviewPortalServer:
    resolved_db = Path(db_path).expanduser().resolve()
    _ensure_opening_review_schema(resolved_db)
    customer_index, customer_source_synced_at = _load_customer_index(
        customer_data_path,
        hmac_secret_path,
    )
    hosts = {"127.0.0.1", "localhost", "localhost.localdomain", "::1", *allowed_hosts}
    return ReviewPortalServer(
        (host, int(port)),
        db_path=resolved_db,
        run_id=run_id,
        allowed_hosts=tuple(hosts),
        customer_index=customer_index,
        customer_source_synced_at=customer_source_synced_at,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the internal sales-profile review workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--run-id", default="latest")
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--customer-data", default=str(DEFAULT_CUSTOMER_DATA_PATH))
    parser.add_argument("--hmac-secret-file", default=str(DEFAULT_HMAC_SECRET_PATH))
    args = parser.parse_args(argv)
    server = create_server(
        args.host,
        args.port,
        db_path=Path(args.db),
        run_id=args.run_id,
        allowed_hosts=args.allowed_host,
        customer_data_path=Path(args.customer_data),
        hmac_secret_path=Path(args.hmac_secret_file),
    )
    print("review portal listening on http://%s:%d" % server.server_address, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
