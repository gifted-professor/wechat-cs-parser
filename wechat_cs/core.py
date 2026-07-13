"""Deterministic analysis primitives for the local WeChat export.

No function in this module performs network I/O.  Customer and sample IDs are
HMACs, while text retained for style retrieval is redacted before persistence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


DEFAULT_HMAC_SECRET = "wechat-cs-development-only-change-me"


def hmac_id(secret: str, kind: str, *parts: object, length: int = 24) -> str:
    """Return a stable, account-scoped opaque identifier."""

    payload = "\x1f".join(str(part) for part in (kind,) + parts).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return "%s_%s" % (kind, digest[:length])


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def parse_timestamp(value: str) -> datetime:
    value = (value or "").strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Message:
    message_key: str
    customer_key: str
    role: str
    timestamp: str
    text: str
    source_file: str
    source_ordinal: int

    @property
    def at(self) -> datetime:
        return parse_timestamp(self.timestamp)


@dataclass
class Turn:
    customer_key: str
    role: str
    started_at: str
    ended_at: str
    text: str
    message_keys: List[str]

    @property
    def start(self) -> datetime:
        return parse_timestamp(self.started_at)

    @property
    def end(self) -> datetime:
        return parse_timestamp(self.ended_at)


@dataclass
class PairCandidate:
    customer_key: str
    trigger_text: str
    reply_text: str
    context: List[Dict[str, str]]
    trigger_keys: List[str]
    reply_keys: List[str]
    timestamp: str
    intent_stage: str
    risk_flags: List[str]
    risk_level: str
    quality_score: int


_REDACTION_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    (
        "address",
        re.compile(r"(?:详细地址|收货地址|所在地区)\s*[:：]\s*[^，,。；;\n]{1,80}"),
    ),
    (
        "phone",
        re.compile(r"(?:手机号码|联系电话|联系电话号码)\s*[:：]\s*[^，,。；;\n]{1,40}"),
    ),
    (
        "person_name",
        re.compile(r"(?:联系人|收件人|姓名)\s*[:：]\s*[^，,。；;\n]{1,24}"),
    ),
    ("id_card", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    (
        "address",
        re.compile(
            r"(?:[\u4e00-\u9fff]{2,}(?:省|自治区|市|自治州|县|区)){1,4}"
            r"[\u4e00-\u9fffA-Za-z0-9-]{0,30}(?:街道|镇|乡|路|街|巷|小区|村|号|栋|幢|单元|室)"
            r"[\u4e00-\u9fffA-Za-z0-9-]{0,20}"
        ),
    ),
    (
        "person_name",
        re.compile(r"(?:收件人|联系人|姓名)\s*[:：]?\s*[\u4e00-\u9fff·]{2,8}"),
    ),
    (
        "person_name",
        re.compile(r"[\u4e00-\u9fff·]{2,6}(?:先生|女士|老师|老板)(?![\u4e00-\u9fff])"),
    ),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)(?:0\d{2,3}[- ]?)?\d{7,8}(?!\d)")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("url", re.compile(r"https?://[^\s]+", re.IGNORECASE)),
    (
        "wechat_id",
        re.compile(r"(?i)(?:(?:微信|wx|wechat)(?:号|id)?\s*[:：]?\s*)[A-Za-z][-_A-Za-z0-9]{5,19}"),
    ),
    ("money", re.compile(r"(?<!\d)(?:¥|￥)?\d+(?:\.\d{1,2})?\s*(?:元|块|rmb|RMB)")),
    (
        "date",
        re.compile(r"(?<!\d)(?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}(?:日|号)?(?!\d)"),
    ),
    (
        "tracking_or_order_id",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{0,4}\d[A-Za-z0-9-]{9,23}(?![A-Za-z0-9])"),
    ),
    ("inventory_state", re.compile(r"(?:有现货|现货充足|有货|没货|无货|缺货|补货中|正在补货)")),
    (
        "logistics_state",
        re.compile(r"(?:已经?|刚刚)?(?:发货|揽收|派送|签收|退回|拦截)(?:了|中)?"),
    ),
    (
        "refund_decision",
        re.compile(r"(?:可以|不能|不支持|同意|会|马上|现在)?(?:给您|给你)?(?:退款|退钱|退货退款)"),
    ),
    (
        "delivery_promise",
        re.compile(r"(?:保证|肯定|一定)(?:可以|会|能)?[^，。！？\n]{0,12}(?:到|送达|收到)"),
    ),
)

_REDACTION_LABELS = {
    "id_card": "[身份证]",
    "address": "[地址]",
    "person_name": "[姓名]",
    "phone": "[手机号]",
    "email": "[邮箱]",
    "url": "[链接]",
    "wechat_id": "[微信号]",
    "money": "[金额]",
    "date": "[日期]",
    "tracking_or_order_id": "[单号]",
    "inventory_state": "[库存状态]",
    "logistics_state": "[物流状态]",
    "refund_decision": "[退款结论]",
    "delivery_promise": "[时效承诺]",
}

_MAINLAND_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def extract_mainland_phones(text: str) -> List[str]:
    """Extract unique mainland mobile numbers for in-memory binding analysis."""

    return sorted(set(_MAINLAND_PHONE.findall(text or "")))


def redact_text(text: str) -> Tuple[str, List[str]]:
    """Redact common PII and volatile values, returning flags as evidence."""

    result = (text or "").replace("\x00", "").strip()
    flags: Set[str] = set()
    for flag, pattern in _REDACTION_PATTERNS:
        result, count = pattern.subn(_REDACTION_LABELS[flag], result)
        if count:
            flags.add(flag)
    # Over-redact address-like fragments that survived the structured pattern.
    # False positives are preferable to allowing an address into training data.
    loose_address = re.compile(
        r"[^，。！？\n]{0,40}(?:省|市|区|县|镇|街道|路|号|小区|楼|室)[^，。！？\n]{0,40}"
    )

    def replace_loose_address(match: re.Match) -> str:
        if re.search(r"\d", match.group(0)):
            flags.add("address")
            return "[地址]"
        return match.group(0)

    result = loose_address.sub(replace_loose_address, result)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result[:4000], sorted(flags)


_RISK_KEYWORDS = {
    "price": ("价格", "价钱", "多少钱", "报价", "优惠", "折扣", "便宜", "贵", "付款", "定金"),
    "inventory": ("库存", "现货", "缺货", "有货", "补货"),
    "refund": ("退款", "退钱", "退货", "撤销", "取消订单"),
    "compensation": ("赔偿", "赔付", "补偿", "红包"),
    "logistics": ("物流", "快递", "单号", "发货", "签收", "派送", "丢件"),
    "delivery_promise": ("保证到", "一定到", "肯定到", "今天到", "明天到", "准时到"),
    "complaint": ("投诉", "欺骗", "骗人", "曝光", "差评", "维权"),
    "quality": ("破损", "损坏", "坏了", "质量", "瑕疵", "少件", "错发", "漏发"),
}


def risk_profile(*texts: str) -> Tuple[List[str], str]:
    joined = "\n".join(texts)
    flags: Set[str] = set()
    for flag, words in _RISK_KEYWORDS.items():
        if any(word in joined for word in words):
            flags.add(flag)
    _, redaction_flags = redact_text(joined)
    if redaction_flags:
        flags.add("pii_or_dynamic_value")
    if "complaint" in flags and ("compensation" in flags or "refund" in flags):
        level = "critical"
    elif flags.intersection(
        {"refund", "compensation", "inventory", "delivery_promise", "complaint"}
    ):
        level = "high"
    elif flags:
        level = "medium"
    else:
        level = "low"
    return sorted(flags), level


_AFTERSALES_P0 = ("退款", "退钱", "投诉", "维权", "欺骗", "骗人", "曝光", "差评")
_AFTERSALES_P1 = (
    "破损",
    "损坏",
    "坏了",
    "质量",
    "瑕疵",
    "错发",
    "漏发",
    "少件",
    "物流异常",
    "没收到",
    "丢件",
)
_AFTERSALES_P2 = ("物流", "快递", "发货", "到哪", "单号", "签收", "售后", "退换")
_CLOSURE_WORDS = ("解决了", "收到了", "没问题了", "可以了", "好的谢谢", "谢谢处理")


def classify_intent(text: str) -> str:
    if any(word in text for word in _AFTERSALES_P0 + _AFTERSALES_P1 + _AFTERSALES_P2):
        return "aftersales"
    if any(
        word in text
        for word in (
            "想要",
            "想买",
            "下单",
            "购买",
            "多少钱",
            "价格",
            "报价",
            "有货",
            "库存",
            "怎么订",
            "付款",
        )
    ):
        return "presales"
    return "general"


def merge_turns(messages: Sequence[Message], window_minutes: int = 15) -> List[Turn]:
    """Merge only consecutive same-role messages inside the time window."""

    if not messages:
        return []
    ordered = sorted(messages, key=lambda item: (item.timestamp, item.source_ordinal))
    turns: List[Turn] = []
    for message in ordered:
        if turns:
            previous = turns[-1]
            gap = (message.at - previous.end).total_seconds()
            if (
                previous.role == message.role
                and gap >= 0
                and gap <= window_minutes * 60
            ):
                previous.ended_at = message.timestamp
                previous.text = "%s\n%s" % (previous.text, message.text)
                previous.message_keys.append(message.message_key)
                continue
        turns.append(
            Turn(
                customer_key=message.customer_key,
                role=message.role,
                started_at=message.timestamp,
                ended_at=message.timestamp,
                text=message.text,
                message_keys=[message.message_key],
            )
        )
    return turns


_LOW_VALUE_REPLIES = {
    "嗯",
    "恩",
    "哦",
    "好",
    "好的",
    "行",
    "可以",
    "收到",
    "知道了",
    "谢谢",
    "不客气",
    "哈哈",
    "？",
    "?",
}


def _pair_quality(trigger: str, reply: str) -> int:
    trigger_clean = re.sub(r"\s+", "", trigger)
    reply_clean = re.sub(r"\s+", "", reply)
    score = 0
    if 4 <= len(trigger_clean) <= 500:
        score += 3
    if 5 <= len(reply_clean) <= 500:
        score += 4
    if reply_clean not in _LOW_VALUE_REPLIES:
        score += 3
    if any(mark in trigger for mark in ("?", "？", "吗", "怎么", "什么", "多少", "能不能")):
        score += 1
    if len(set(reply_clean)) >= 4:
        score += 1
    return score


def pair_turns(turns: Sequence[Turn], reply_window_minutes: int = 30) -> List[PairCandidate]:
    candidates: List[PairCandidate] = []
    seen: Set[str] = set()
    for index in range(len(turns) - 1):
        trigger = turns[index]
        reply = turns[index + 1]
        if trigger.role != "customer" or reply.role != "studio":
            continue
        gap = (reply.start - trigger.end).total_seconds()
        if gap < 0 or gap > reply_window_minutes * 60:
            continue
        quality = _pair_quality(trigger.text, reply.text)
        if quality < 8:
            continue
        trigger_redacted, trigger_redactions = redact_text(trigger.text)
        reply_redacted, reply_redactions = redact_text(reply.text)
        normalized = re.sub(r"\s+", "", trigger_redacted + "\x1f" + reply_redacted).lower()
        duplicate_key = content_digest(normalized)
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        context: List[Dict[str, str]] = []
        for previous in turns[max(0, index - 2) : index]:
            redacted, _ = redact_text(previous.text)
            context.append(
                {"role": previous.role, "text": redacted, "timestamp": previous.ended_at}
            )
        risk_flags, risk_level = risk_profile(trigger.text, reply.text)
        if trigger_redactions or reply_redactions:
            risk_flags = sorted(set(risk_flags).union({"redacted_value"}))
        candidates.append(
            PairCandidate(
                customer_key=trigger.customer_key,
                trigger_text=trigger_redacted,
                reply_text=reply_redacted,
                context=context,
                trigger_keys=list(trigger.message_keys),
                reply_keys=list(reply.message_keys),
                timestamp=reply.ended_at,
                intent_stage=classify_intent(trigger.text),
                risk_flags=risk_flags,
                risk_level=risk_level,
                quality_score=quality,
            )
        )
    return candidates


def stable_split(secret: str, customer_key: str) -> str:
    """Stable group split: a customer can never cross dataset partitions."""

    digest = hmac.new(secret.encode("utf-8"), customer_key.encode("utf-8"), hashlib.sha256)
    bucket = int(digest.hexdigest()[:8], 16) % 100
    if bucket < 70:
        return "train"
    if bucket < 85:
        return "validation"
    return "test"


def _template_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = re.sub(r"\[[^\]]+\]", "[值]", normalized)
    normalized = re.sub(r"\d+", "#", normalized)
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff\[\]#]+", "", normalized)
    return content_digest(normalized)


def select_candidates(
    candidates: Sequence[PairCandidate],
    limit: int = 500,
    secret: Optional[str] = None,
    maximum_per_reply_template: int = 3,
) -> List[PairCandidate]:
    """Globally deduplicate and round-robin customers.

    A normalized reply template is restricted to one customer split so common
    canned replies cannot leak from train into validation/test.  Its frequency
    is also capped to keep the corpus representative of style rather than a
    single operational macro.
    """

    pair_unique: List[PairCandidate] = []
    seen_pairs: Set[str] = set()
    for candidate in sorted(
        candidates, key=lambda item: (-item.quality_score, item.timestamp, item.customer_key)
    ):
        pair_key = _template_key(candidate.trigger_text + "\x1f" + candidate.reply_text)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        pair_unique.append(candidate)

    template_groups: Dict[str, List[PairCandidate]] = {}
    for candidate in pair_unique:
        template_groups.setdefault(_template_key(candidate.reply_text), []).append(candidate)
    unique: List[PairCandidate] = []
    assigned = {"train": 0, "validation": 0, "test": 0}
    ratios = {"train": 0.70, "validation": 0.15, "test": 0.15}
    for template_key in sorted(
        template_groups,
        key=lambda key: (-max(item.quality_score for item in template_groups[key]), key),
    ):
        group = template_groups[template_key]
        if secret:
            available: Dict[str, List[PairCandidate]] = {}
            for candidate in group:
                available.setdefault(stable_split(secret, candidate.customer_key), []).append(candidate)
            bucket = int(template_key[:8], 16) % 100
            preferred = "train" if bucket < 70 else "validation" if bucket < 85 else "test"
            owner = min(
                available,
                key=lambda name: (
                    assigned[name] / ratios[name],
                    0 if name == preferred else 1,
                    name,
                ),
            )
            chosen = available[owner]
            assigned[owner] += min(len(chosen), max(1, maximum_per_reply_template))
        else:
            owner_customer = group[0].customer_key
            chosen = [item for item in group if item.customer_key == owner_customer]
        unique.extend(chosen[: max(1, maximum_per_reply_template)])

    by_customer: Dict[str, List[PairCandidate]] = {}
    for candidate in unique:
        by_customer.setdefault(candidate.customer_key, []).append(candidate)
    for items in by_customer.values():
        items.sort(key=lambda item: (-item.quality_score, item.timestamp))
    def round_robin(customer_items: Dict[str, List[PairCandidate]]) -> List[PairCandidate]:
        output: List[PairCandidate] = []
        customers = sorted(customer_items)
        offset = 0
        while True:
            added = False
            for customer_key in customers:
                items = customer_items[customer_key]
                if offset < len(items):
                    output.append(items[offset])
                    added = True
            if not added:
                break
            offset += 1
        return output

    if secret and limit > 0:
        by_split: Dict[str, Dict[str, List[PairCandidate]]] = {
            "train": {},
            "validation": {},
            "test": {},
        }
        for customer_key, items in by_customer.items():
            by_split[stable_split(secret, customer_key)][customer_key] = items
        sequences = {name: round_robin(items) for name, items in by_split.items()}
        quotas = {
            "train": int(limit * 0.70),
            "validation": int(limit * 0.15),
        }
        quotas["test"] = limit - quotas["train"] - quotas["validation"]
        selected = []
        offsets: Dict[str, int] = {}
        for name in ("train", "validation", "test"):
            take = min(quotas[name], len(sequences[name]))
            selected.extend(sequences[name][:take])
            offsets[name] = take
        # A sparse split must not stop the build; fill its remainder from the
        # other split sequences without changing any customer's assignment.
        while len(selected) < limit:
            added = False
            for name in ("train", "validation", "test"):
                offset = offsets[name]
                if offset < len(sequences[name]):
                    selected.append(sequences[name][offset])
                    offsets[name] = offset + 1
                    added = True
                    if len(selected) >= limit:
                        break
            if not added:
                break
    else:
        selected = round_robin(by_customer)[:limit]
    return sorted(selected, key=lambda item: (item.timestamp, item.customer_key))


def _matching_messages(messages: Sequence[Message], words: Iterable[str]) -> List[Message]:
    keywords = tuple(words)
    return [message for message in messages if any(word in message.text for word in keywords)]


def analyze_customer(messages: Sequence[Message], snapshot_at: datetime) -> Dict[str, object]:
    """Return transparent, deterministic opportunity and after-sales analysis."""

    ordered = sorted(messages, key=lambda item: (item.timestamp, item.source_ordinal))
    customer_messages = [item for item in ordered if item.role == "customer"]
    studio_messages = [item for item in ordered if item.role == "studio"]
    if not ordered:
        return {}

    purchase_words = ("想要", "想买", "下单", "购买", "怎么订", "付款", "定金", "订一个")
    price_words = ("多少钱", "价格", "报价", "优惠", "便宜", "预算")
    urgency_words = ("今天", "明天", "尽快", "急", "马上", "什么时候能", "来得及")
    repeat_words = ("再来", "再买", "回购", "还是之前", "又要", "老样子")
    rejection_words = ("不需要", "不用了", "不买", "算了", "暂时不要", "已经买了")
    objection_words = ("太贵", "考虑一下", "担心", "但是", "能便宜", "预算", "不确定")
    preference_words = ("喜欢", "想要", "不要", "颜色", "款式", "尺寸", "偏好")
    commitment_words = ("我帮你", "给你查", "稍后", "等我", "马上处理", "会给", "可以安排")

    purchase = _matching_messages(customer_messages, purchase_words + price_words)
    urgency = _matching_messages(customer_messages, urgency_words)
    repeat = _matching_messages(customer_messages, repeat_words)
    rejection = _matching_messages(customer_messages[-10:], rejection_words)
    objections = _matching_messages(customer_messages[-20:], objection_words)
    preferences = _matching_messages(customer_messages[-20:], preference_words)
    commitments = _matching_messages(studio_messages[-20:], commitment_words)

    last_at = ordered[-1].at
    recency_days = max(0, int((snapshot_at - last_at).total_seconds() // 86400))
    purchase_points = min(35, len(purchase) * 9)
    urgency_points = min(20, len(urgency) * 10)
    if recency_days <= 3:
        urgency_points = min(20, urgency_points + 8)
    elif recency_days <= 14:
        urgency_points = min(20, urgency_points + 4)
    interaction_points = min(15, len(customer_messages) // 3 + min(8, len(merge_turns(ordered)) // 4))

    last_role_customer = ordered[-1].role == "customer"
    has_open_question = bool(
        customer_messages
        and any(mark in customer_messages[-1].text for mark in ("?", "？", "吗", "怎么", "多少", "能不能"))
    )
    followup_points = 0
    if last_role_customer:
        followup_points += 8
    if purchase and (last_role_customer or has_open_question):
        followup_points += 7
    followup_points = min(15, followup_points)

    duration_days = max(0, int((ordered[-1].at - ordered[0].at).total_seconds() // 86400))
    recurrence_points = min(15, len(repeat) * 7 + (4 if duration_days >= 30 else 0))
    rejection_penalty = 30 if rejection else 0
    score = max(
        0,
        min(
            100,
            purchase_points
            + urgency_points
            + interaction_points
            + followup_points
            + recurrence_points
            - rejection_penalty,
        ),
    )
    level = "high" if score >= 60 else "medium" if score >= 35 else "low"

    # Only a customer acknowledgement closes an incident.  This conservative
    # rule prevents an agent promise from silently hiding an unresolved case.
    last_incident: Optional[Message] = None
    priority: Optional[str] = None
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    for message in customer_messages:
        if last_incident and any(word in message.text for word in _CLOSURE_WORDS):
            last_incident, priority = None, None
            continue
        incident_priority: Optional[str] = None
        if any(word in message.text for word in _AFTERSALES_P0):
            incident_priority = "P0"
        elif any(word in message.text for word in _AFTERSALES_P1):
            incident_priority = "P1"
        elif any(word in message.text for word in _AFTERSALES_P2):
            incident_priority = "P2"
        if incident_priority and (
            priority is None or priority_rank[incident_priority] <= priority_rank[priority]
        ):
            last_incident, priority = message, incident_priority
    queue = "aftersales" if priority else "presales"

    def keys(items: Sequence[Message], maximum: int = 5) -> List[str]:
        return [item.message_key for item in items[-maximum:]]

    reasons = [
        {"code": "purchase_intent", "points": purchase_points, "evidence": keys(purchase)},
        {"code": "time_urgency", "points": urgency_points, "evidence": keys(urgency)},
        {
            "code": "interaction",
            "points": interaction_points,
            "evidence": keys(customer_messages, 3),
        },
        {
            "code": "open_followup",
            "points": followup_points,
            "evidence": keys(customer_messages[-1:] if last_role_customer else []),
        },
        {"code": "recurrence", "points": recurrence_points, "evidence": keys(repeat)},
    ]
    if rejection_penalty:
        reasons.append(
            {"code": "explicit_rejection", "points": -rejection_penalty, "evidence": keys(rejection)}
        )
    if priority and last_incident:
        reasons.append(
            {"code": "unresolved_aftersales_%s" % priority.lower(), "points": 0, "evidence": [last_incident.message_key]}
        )

    recent_context = []
    for message in ordered[-8:]:
        text, _ = redact_text(message.text)
        recent_context.append(
            {
                "message_key": message.message_key,
                "role": message.role,
                "timestamp": message.timestamp,
                "text": text,
            }
        )

    def redacted_entries(items: Sequence[Message], maximum: int = 3) -> List[Dict[str, str]]:
        output = []
        for item in items[-maximum:]:
            text, _ = redact_text(item.text)
            output.append({"message_key": item.message_key, "text": text, "timestamp": item.timestamp})
        return output

    pending = customer_messages[-1:] if last_role_customer else []
    memory = {
        "current_needs": redacted_entries(purchase or customer_messages[-3:]),
        "objections": redacted_entries(objections),
        "preferences": redacted_entries(preferences),
        "pending_followups": redacted_entries(pending),
        "studio_commitments": redacted_entries(commitments),
        "recent_context": recent_context,
    }
    return {
        "last_active_at": ordered[-1].timestamp,
        "opportunity_score": score,
        "opportunity_level": level,
        "aftersales_priority": priority,
        "queue": queue,
        "summary": "%s；%s机会" % ("售后%s" % priority if priority else "售前", level),
        "reasons": reasons,
        "memory": memory,
        "evidence_message_keys": sorted(
            {key for reason in reasons for key in reason.get("evidence", [])}
        ),
    }


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
